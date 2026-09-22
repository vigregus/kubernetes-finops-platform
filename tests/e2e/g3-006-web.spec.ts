import { randomUUID } from "node:crypto"
import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import type { BrowserContext, Page } from "@playwright/test"
import { API, STATE_FILE, fixtureFor, headers, signIn } from "./support/auth"
import type { Fixture, SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G3-006: пять состояний соединения, обнаружение пропуска,
 * синхронизация (`RT-004`, `MSG-005`).
 *
 * Проверка идёт **против стенда и с хоста**: только публичные имена, ни одного
 * обращения внутрь кластера и ни одного туннеля к нему. Ни одного оператора
 * удаления здесь нет и быть не может: уборка живёт в
 * `scripts/web-e2e-fixture.sh` и охватывает сам Playwright.
 *
 * Сценарий — спуск в `recovered=false` управляемым офлайном и барьер публикации.
 * Свидетель (A) и предмет (B) живут в **разных** `BrowserContext`: `setOffline`
 * переводит в офлайн весь контекст, поэтому общий погасил бы вместе с предметом
 * свидетеля, и барьер доказывал бы пустоту.
 *
 * Заморозка вкладки (`frozen`) не применяется: она не даёт ни
 * `navigator.onLine === false`, ни события `offline`, то есть не предъявляет
 * production-вход, введённый для `DISCONNECTED`. Либо `setOffline(true)` даёт
 * наблюдаемый переход, либо сценарий падает громко.
 */

const A: Fixture = fixtureFor("A")
const B: Fixture = fixtureFor("B")

/**
 * Окно восстановления канала — `history_size: 100` (`channels.json`).
 * 101 публикация делает прежнюю позицию невосстановимой **гарантированно**, а не
 * «вероятно»: при 100 самая старая сохранённая публикация равна `S+1`, и
 * восстановление проходит целиком (`recovered=true`, `SYNCING` не наступает).
 */
const BURST = 101

/** Уход в офлайн подтверждается браузерным событием, а не паузой: ждём переход. */
const OFFLINE_TIMEOUT = 30_000

/** Барьер: 101 сообщение идёт Postgres → outbox → Kafka → consumer-realtime → Centrifugo. */
const BARRIER_TIMEOUT = 180_000

const REASON = "recovery-miss"

interface Transition {
	state: string
	reason: string | null
}

/** Что приёмка читает из production-разметки. */
interface Feed {
	state: string | null
	reason: string | null
	boundary: number | null
	ids: string[]
	seqs: number[]
}

interface Sent {
	message_id: string
	seq: number
}

async function readFeed(page: Page): Promise<Feed> {
	return page.evaluate(() => {
		const pane = document.querySelector("[data-connection-state]")
		const list = document.querySelector("[data-applied-through-seq]")
		const nodes = Array.from(document.querySelectorAll("[data-message-id]"))
		const boundary = list?.getAttribute("data-applied-through-seq")

		return {
			state: pane?.getAttribute("data-connection-state") ?? null,
			reason: pane?.getAttribute("data-sync-reason") ?? null,
			boundary: boundary === null || boundary === undefined ? null : Number(boundary),
			ids: nodes.map((node) => node.getAttribute("data-message-id") ?? ""),
			seqs: nodes.map((node) => Number(node.getAttribute("data-message-seq"))),
		}
	})
}

/**
 * Наблюдатель переходов — **до** воздействия, а не опрос в нужный момент.
 *
 * `CONNECTING` может жить миллисекунды: `online` → React `CONNECTING` → SDK
 * `connected`/`subscribed` → `SYNCING`. Чтение атрибута «когда понадобилось»
 * этого не доказывает — состояние успевает уехать. Наблюдатель внедряет сама
 * приёмка, а читает он **реальные** атрибуты продукта: это не test-only шов в
 * production-коде.
 *
 * Пишется переход, а не каждый вызов колбэка: `MutationObserver` срабатывает и
 * на вставку узла, и на изменение соседнего атрибута.
 */
async function observeTransitions(page: Page): Promise<void> {
	await page.evaluate(() => {
		const seen: Array<{ state: string; reason: string | null }> = []

		const record = (): void => {
			const pane = document.querySelector("[data-connection-state]")
			if (pane === null) return
			const next = {
				state: pane.getAttribute("data-connection-state") ?? "",
				reason: pane.getAttribute("data-sync-reason"),
			}
			const last = seen[seen.length - 1]
			if (last !== undefined && last.state === next.state && last.reason === next.reason) return
			seen.push(next)
		}

		record()
		new MutationObserver(record).observe(document.documentElement, {
			subtree: true,
			childList: true,
			attributes: true,
			attributeFilter: ["data-connection-state", "data-sync-reason"],
		})
		;(window as unknown as { __g3_006?: unknown }).__g3_006 = seen
	})
}

async function transitions(page: Page): Promise<Transition[]> {
	return page.evaluate(
		() => (window as unknown as { __g3_006?: Transition[] }).__g3_006 ?? [],
	)
}

const statesOf = (seen: Transition[]): string[] => seen.map((transition) => transition.state)

/** Подпоследовательность, а не равенство: между переходами наблюдатель видит и промежуточные. */
function isSubsequence(expected: string[], actual: string[]): boolean {
	let cursor = 0
	for (const state of actual) {
		if (state === expected[cursor]) cursor += 1
	}
	return cursor === expected.length
}

const render = (seen: Transition[]): string =>
	seen.map((transition) => (transition.reason === null ? transition.state : `${transition.state}(${transition.reason})`)).join(" → ")

/** Ждёт условия, накапливая последнее наблюдение для сообщения об отказе. */
async function until(
	condition: () => Promise<boolean>,
	observe: () => Promise<string>,
	what: string,
	timeout: number,
): Promise<void> {
	const deadline = Date.now() + timeout

	for (;;) {
		if (await condition()) return

		const seen = await observe()
		if (Date.now() >= deadline) {
			throw new Error(`${what}: не дождались за ${timeout} мс. Наблюдённое: ${seen}`)
		}
		await new Promise((resolve) => setTimeout(resolve, 250))
	}
}

/** Штатная отправка: `POST /messages` → Postgres → outbox → Kafka → Centrifugo. */
async function send(
	page: Page,
	token: string,
	fixture: Fixture,
	conversationId: string,
	text: string,
): Promise<Sent> {
	const response = await page.request.post(`${API}/conversations/${conversationId}/messages`, {
		headers: headers(fixture, token),
		data: { client_message_id: randomUUID(), type: "text", payload: { text } },
	})

	// `429` контракт объявляет, но на этом пути обработчик его не применяет.
	// Любой иной код означает, что сообщение не заняло свой `seq` — и сценарий
	// обязан сказать это здесь, а не разбираться с последствиями в сверке burst.
	expect(
		[200, 201],
		`POST /messages («${text}») — сообщение обязано быть принято`,
	).toContain(response.status())

	const body = (await response.json()) as { message_id?: unknown; seq?: unknown }
	expect(typeof body.message_id, "ответ отправки несёт message_id").toBe("string")
	expect(typeof body.seq, "ответ отправки несёт seq").toBe("number")

	return { message_id: body.message_id as string, seq: body.seq as number }
}

test("сон вкладки: recovered=false → SYNCING → догрузка → CONNECTED", async ({ browser }) => {
	const state = JSON.parse(readFileSync(STATE_FILE, "utf8")) as State
	const conversationId = state.conversationId

	const contexts: BrowserContext[] = []

	// Контекст попадает в список сразу после открытия — иначе падение второго
	// входа оставило бы первый открытым, а уборка снова оказалась бы полумерой.
	const open = async (fixture: Fixture): Promise<SignedIn> => {
		const signedIn = await signIn(browser, fixture)
		contexts.push(signedIn.context)
		return signedIn
	}

	try {
		const a = await open(A)
		const b = await open(B)
		const contextB = b.context

		const paneB = b.page.locator("[data-connection-state]")

		await test.step("обе стороны открывают беседу, и у обеих лента отрисована", async () => {
			for (const [who, signedIn] of [
				["A", a],
				["B", b],
			] as const) {
				await signedIn.page.locator(`[data-conversation-id="${conversationId}"]`).click()

				await expect(
					signedIn.page.locator("[data-connection-state]"),
					`сторона ${who}: панель беседы несёт data-connection-state — поверхность G3-006`,
				).toHaveAttribute("data-connection-state", "connected", { timeout: 60_000 })

				// Снимок применён — иначе границы нет, и наблюдать было бы нечего.
				await expect(
					signedIn.page.locator("[data-applied-through-seq]"),
					`сторона ${who}: снимок хвоста применён и граница названа`,
				).toHaveAttribute("data-applied-through-seq", /^\d+$/)
			}
		})

		// Наблюдатель ставится после первого CONNECTED: до него запись начиналась
		// бы с `connecting`, и утверждение «свидетель не покидал connected» было
		// бы утверждением о моменте установки, а не о ходе сценария.
		await observeTransitions(a.page)
		await observeTransitions(b.page)

		const head0 = (await readFeed(b.page)).boundary
		expect(head0, "граница предмета известна до live-probe").not.toBeNull()

		const probe = await test.step("живость канала: сообщение A доходит до B без перезагрузки", async () => {
			const sent = await send(a.page, a.token, A, conversationId, "live probe")

			await expect(
				b.page.locator(`[data-message-id="${sent.message_id}"]`),
				"B видит сообщение A без перезагрузки — воздействие не доказывает пустоту",
			).toHaveCount(1, { timeout: 60_000 })

			return sent
		})

		const S = await test.step("S назначается после live-probe", async () => {
			// Live-probe занимает `seq = head₀ + 1`. Возьми S раньше — и burst
			// был бы `S+2 … S+102`, а все утверждения про `S+1 … S+101` неверны
			// на единицу.
			expect(
				probe.seq,
				"live-probe занимает ровно следующий номер за границей предмета",
			).toBe((head0 as number) + 1)

			const feed = await readFeed(b.page)
			expect(feed.boundary, "S — граница предмета, взятая после live-probe").toBe(probe.seq)
			return probe.seq
		})

		await test.step("предмет уходит в офлайн, и это наблюдаемо", async () => {
			await contextB.setOffline(true)

			await expect(
				paneB,
				"DISCONNECTED от браузерного события offline: уход обязан быть наблюдаемым, " +
					"иначе шаг молча пропущен, и приёмка доказывает пустоту",
			).toHaveAttribute("data-connection-state", "disconnected", { timeout: OFFLINE_TIMEOUT })
		})

		const sent: Sent[] = []

		await test.step(`${BURST} сообщений штатным путём, ровно S+1 … S+${BURST}`, async () => {
			for (let index = 0; index < BURST; index += 1) {
				sent.push(await send(a.page, a.token, A, conversationId, `burst ${S + index + 1}`))
			}

			expect(
				sent.map((message) => message.seq),
				"ответы отправки подтверждают смещение: burst занимает ровно S+1 … S+101",
			).toEqual(Array.from({ length: BURST }, (_, index) => S + index + 1))
		})

		await test.step("барьер публикации: свидетель дошёл до S+101, пока предмет был офлайн", async () => {
			const expected = sent.map((message) => message.message_id)

			// Барьер доказывается **положительно**: B не возвращается в сеть, пока
			// A не достиг цели. Все четыре условия сразу — состояние не покидало
			// `connected`, `syncing` не наблюдался, все ожидаемые `message_id` в
			// ленте, граница равна `S+101` — и говорят, что 101 сообщение прошло
			// Centrifugo publication path, а не что A дочитал их через REST.
			await until(
				async () => {
					const feed = await readFeed(a.page)
					return feed.boundary === S + BURST && expected.every((id) => feed.ids.includes(id))
				},
				async () => {
					const feed = await readFeed(a.page)
					const missing = expected.filter((id) => !feed.ids.includes(id)).length
					return (
						`свидетель: состояние ${feed.state}, граница ${feed.boundary} (ждём ${S + BURST}), ` +
						`недостаёт ${missing} из ${BURST}, наблюдалось ${render(await transitions(a.page))}`
					)
				},
				"барьер публикации",
				BARRIER_TIMEOUT,
			)
		})

		await test.step("свидетель не покидал CONNECTED и не входил в SYNCING", async () => {
			// Без этого условия барьер говорил бы лишь «A получил данные»:
			// дочитав их своей догрузкой, A тоже дошёл бы до `S+101`.
			const seen = statesOf(await transitions(a.page))
			expect(
				seen,
				"свидетель видел только connected: 101 сообщение пришло каналом, " +
					"а не догрузкой по REST",
			).toEqual(["connected"])
		})

		await test.step("предмет возвращается в сеть: CONNECTING → SYNCING → CONNECTED", async () => {
			await contextB.setOffline(false)

			await until(
				async () =>
					isSubsequence(
						["disconnected", "connecting", "syncing", "connected"],
						statesOf(await transitions(b.page)),
					),
				async () => render(await transitions(b.page)),
				"переходы предмета после возврата сети",
				BARRIER_TIMEOUT,
			)
		})

		await test.step("причина синхронизации названа", async () => {
			// Наблюдаемое `SYNCING` с `recovery-miss` и есть доказательство
			// `recovered=false`: при `recovered=true` состояние осталось бы
			// `CONNECTED`. Имя причины отличает расхождение восстановления от
			// локального пропуска в `seq` — другого production-входа в SYNCING
			// у этого сценария нет.
			const syncing = (await transitions(b.page)).filter(
				(transition) => transition.state === "syncing",
			)
			expect(syncing.length, "SYNCING наблюдался").toBeGreaterThan(0)
			expect(
				syncing.map((transition) => transition.reason),
				"причина у каждого SYNCING — расхождение восстановления",
			).toEqual(syncing.map(() => REASON))
		})

		await test.step("догрузка сошлась на границе S+101", async () => {
			await expect(paneB).toHaveAttribute("data-connection-state", "connected", {
				timeout: BARRIER_TIMEOUT,
			})
			await expect(
				b.page.locator("[data-applied-through-seq]"),
				"граница применённого дошла до головы burst",
			).toHaveAttribute("data-applied-through-seq", String(S + BURST), {
				timeout: BARRIER_TIMEOUT,
			})
		})

		await test.step("вся лента монотонна по seq и уникальна по message_id", async () => {
			const feed = await readFeed(b.page)
			expect(feed.seqs.length, "лента не пуста").toBeGreaterThan(0)
			expect(new Set(feed.ids).size, "идентификаторы уникальны во всей ленте").toBe(feed.ids.length)

			for (let index = 1; index < feed.seqs.length; index += 1) {
				expect(
					feed.seqs[index],
					`порядок ленты на позиции ${index} (seq ${feed.seqs.join(", ")})`,
				).toBeGreaterThan(feed.seqs[index - 1])
			}
		})

		await test.step("burst сверен с измеренным expected из ответов POST", async () => {
			// Уникальности самой по себе недостаточно: «все идентификаторы разные»
			// верно и тогда, когда конкретное сообщение не доехало вовсе. Поэтому
			// сверка идёт с `expected`, собранным из ответов отправки.
			const feed = await readFeed(b.page)
			const positions = new Map(feed.ids.map((id, index) => [id, index]))

			for (const message of sent) {
				const position = positions.get(message.message_id) ?? -1
				expect(
					position,
					`сообщение ${message.message_id} присутствует в ленте`,
				).toBeGreaterThanOrEqual(0)
				expect(
					feed.seqs[position],
					`seq сообщения ${message.message_id}`,
				).toBe(message.seq)
			}

			const range = feed.seqs.filter((seq) => seq >= S + 1 && seq <= S + BURST)
			expect(
				range,
				"диапазон S+1 … S+101 занят ровно этим набором, каждое значение один раз",
			).toEqual(Array.from({ length: BURST }, (_, index) => S + index + 1))
		})

		const feed = await readFeed(b.page)
		console.log(
			`  G3-006: S = ${S}, граница = ${feed.boundary}, в ленте ${feed.ids.length} сообщений; ` +
				`отправлено 1 (live-probe) + ${BURST} (seq ${S + 1} … ${S + BURST}), причина ${REASON}`,
		)
	} finally {
		// Контексты закрываются **до** уборки скрипта: это шаг 1 её порядка
		// (`contexts → logout в Keycloak → realtime_connections → sessions`).
		for (const context of contexts) await context.close()
	}
})
