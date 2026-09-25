import { randomUUID } from "node:crypto"
import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import type { BrowserContext, Page } from "@playwright/test"
import {
	API,
	IDP_ORIGIN,
	STATE_FILE,
	fixtureFor,
	headers,
	signIn,
	signInTab,
} from "./support/auth"
import type { Fixture, SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G3-007: квитанции, непрочитанные, несколько вкладок,
 * присутствие (`RCP-001`, `RCP-002`, `CTR-003`, `PRS-*`).
 *
 * Проверка идёт **против стенда и с хоста**: только публичные имена, ни одного
 * обращения внутрь кластера и ни одного туннеля к нему. Ни одного оператора
 * удаления здесь нет и быть не может: уборка живёт в
 * `scripts/web-e2e-fixture.sh` и охватывает сам Playwright.
 *
 * Числа берутся из production-разметки, а не из проверки: `data-my-read-seq` и
 * `data-peer-read-seq` — то, что вкладка **сообщила** и что сообщил собеседник,
 * `data-unread-count` — счётчик строки списка, `data-message-state` — состояние
 * **своего** сообщения по квитанции, `data-presence` — присутствие в шапке.
 * Каждый из них назван в докстринге тем, что доказывает; приёмка читает их, а не
 * текст интерфейса.
 *
 * ## Фоновая вкладка: измерено, и из спеки снято явной строкой
 *
 * `D6`/`D13` опираются на то, что фоновая вкладка докладывает `hidden`. В
 * headless-браузере это известное место расхождения с headed, поэтому измерено
 * прямо, разовой меркой на двух страницах одного контекста: после
 * `bringToFront(B)` **обе** страницы докладывают `visible`, и на A не приходит ни
 * одного события `visibilitychange`. Исход тот же в headless, в headed и со
 * снятым `--disable-backgrounding-occluded-windows`: гипотеза «дело во флаге
 * запуска» опровергнута, а не подкреплена.
 *
 * Поэтому применяется названный в плане откат: нога «в фоне `read` не растёт»
 * остаётся **юнитом** (`useReceipts`, срез 6), а из этой спеки она снимается
 * **явной строкой, а не тишиной** — ни `bringToFront`, ни `visibilityState`
 * здесь нет ни в одной строке кода, и запрет на них стоит инвариантом в
 * `scripts/web-e2e-fixture.sh`. Иначе снятая нога вернулась бы молча, и
 * «приёмка ничего про фон не утверждает» стало бы неотличимо от «приёмка про фон
 * забыла».
 *
 * Сверка при возврате (`D11`) от этого не теряется: её второй повод — не
 * видимость, а **связь**. `ConversationPane` зовёт `onReconcile` на выходе из
 * `disconnected`/`degraded`, и это тот же самый production-обработчик.
 * `setOffline` — воздействие, которое браузер действительно производит, в
 * отличие от смены видимости, которой он в headless не производит вовсе.
 *
 * ## Две вкладки: две страницы **одного** контекста — и это измерено здесь же
 *
 * `RCP-002` требует двух вкладок одного человека. Выбрана укладка «две страницы
 * одного контекста», и не по удобству: `addInitScript` задан **контексту**, а не
 * странице (`support/auth.ts::signIn`), поэтому обе страницы встают на один и тот
 * же `device_id` и на одну сессию — по `0006_realtime_connections.sql` это и есть
 * «одна сессия живёт сразу в нескольких вкладках» (ключ `client_id`, он же у
 * вкладки свой).
 *
 * **Как входит вторая вкладка — измерено, и первая догадка была неверна.**
 * Ожидалось, что она пройдёт тем же путём, что первая: кнопка входа, форма
 * удостоверяющего центра, возврат с кодом (cookie общие, поэтому без формы).
 * Прогон опроверг: кнопки на второй странице **нет вовсе**, форма не появляется,
 * запросов к `idp` не уходит, а страница сама зовёт `POST /api/v1/auth/refresh` и
 * получает `200` — удостоверяющего центра она не касается. Это не наблюдение
 * разметки, а объявленное поведение: «Токен доступа живёт в памяти вкладки и
 * исчезает при перезагрузке, поэтому клиент при старте обменяет cookie на новый.
 * Несколько вкладок обменяют каждая свой, и одновременные запросы - норма, а не
 * признак подбора» (`openapi.yaml`, `/auth/refresh`). Поэтому `signInTab` ждёт
 * **оба** обмена и навигирует сама, а кнопка — необязательная ветка для страницы
 * без cookie.
 *
 * Следствие для предмета: в контексте оказываются **два** рукопожатия
 * `GET /connection/websocket → 101` (видно в трейсе), то есть два `client_id`
 * под одним `session_id` — ровно то, что называет комментарий `0006`. Это и есть
 * проверяемое здесь: обе страницы живут одновременно, обе в `connected`, запрос
 * первой продолжает отвечать `200`, публикации беседы видят обе.
 *
 * Второй способ (второй контекст на ту же запись) **не используется**, потому что
 * он не измерен: комментарий `workers: 1` в `playwright.config.ts` утверждал, что
 * такой вход снял бы сессию первого, и это утверждение было непроверенным. Оно
 * исправлено, а не унаследовано.
 *
 * ## Что показал первый живой прогон (2026-09-25): приёмка ещё не состоялась
 *
 * **8а прошёл**, и прошёл по существу: три страницы одного контекста, все в
 * `connected`, одновременная отправка заняла `seq 307` и `308`, лента `257…308`
 * совпала у A, B1, B2 **и** с порядком из REST. Укладка вкладок и сходимость
 * ленты предъявлены на стенде, а не описаны. Вход второй вкладки при этом стал
 * утверждением, а не печатью: сценарий требует, чтобы она не сделала к
 * удостоверяющему центру **ни одного** запроса (`secondTab`).
 *
 * **Три оставшиеся ноги падают**, и причина у всех одна: стенд отдаёт сборку
 * `main`, в которой нет ни одной поверхности этого гейта. Это проверено, а не
 * выведено: в бандле, который отдаёт `https://app.finops.local` (сборка
 * `index-BJ4Nc9PU.js`), `data-applied-through-seq` и `data-connection-state` есть — то
 * есть поверхности `G3-006` в нём на месте, — а `data-presence`,
 * `data-unread-count`, `data-peer-read-seq`, `data-my-read-seq` и
 * `data-message-state` не встречаются ни разу. Отстаёт не только веб: нагрузки
 * стенда запущены образом `messenger-api@sha256:6810735…` (читается из
 * `kubectl -n messenger get deploy`), а он старше серверных коммитов этой ветки —
 * значит, `online`, `read_states` и публикаций `unread.changed` на стенде нет
 * тоже. Диджест в `main` приносит обе половины одним движением (D9), и 8а зелен
 * ровно потому, что утверждает только о том, что в нынешней сборке есть.
 *
 * Падение этой причины узнаётся по подписи: оно приходит на **первом** чтении
 * новой поверхности (`element(s) not found` для `data-presence`, `read=null` для
 * `data-my-read-seq`, `Received: null` для `data-message-state`), а не на
 * утверждении о числе. Названо это затем, чтобы краснота не читалась ни как
 * дефект спеки, ни как «приёмка прошла»: **приёмка не состоялась**, и состояться
 * до диджеста не может.
 *
 * ## Что повторено из `g3-006`, и почему это копия, а не общий модуль
 *
 * `readSurface`, `until` и `send` — копии одноимённых помощников соседней спеки.
 * Вынести их в `support/` значило бы править файл, живое доказательство которого
 * уже предъявлено и **не может быть повторено** (стенд отдаёт бандл из `main`, и
 * до диджеста нового прогона не будет). Правка закрытой приёмки ради экономии
 * тридцати строк — цена, которую здесь не платят; расхождение копий названо тем,
 * чем оно является.
 *
 * Сценарии: 8а — одновременная отправка двух вкладок и сходимость ленты;
 * 8б — непрочитанное событием и сверкой; присутствие — предикат времени, а не
 * состояние сокета; 9 — квитанция вперёд, до собеседника и из REST. Прогон
 * печатает измеренные числа, а не вывод «совпало».
 */

const A: Fixture = fixtureFor("A")
const B: Fixture = fixtureFor("B")

/** Соединение беседы поднимается после клика: панель обязана дойти до `connected`. */
const CONNECT_TIMEOUT = 60_000

/** Сообщение идёт Postgres → outbox → Kafka → consumer-realtime → Centrifugo → браузер. */
const PUBLICATION_TIMEOUT = 120_000

/** Уход в офлайн подтверждается браузерным событием, а не паузой: ждём переход. */
const OFFLINE_TIMEOUT = 30_000

/**
 * Предел наполнения ленты в 8б: сколько сообщений шлём, пока лента не перерастёт
 * окно. Предел, а не «сколько нужно»: условие выхода — измеренное, а этот счётчик
 * только не даёт циклу идти вечно, если условие никогда не наступит.
 */
const FILL_CAP = 40

/** Сколько сообщений A шлёт под событийную ногу 8б. */
const ARRIVED = 3

/** Сколько уходит, пока получатель офлайн: их событий вкладки не увидят. */
const MISSED = 2

/**
 * Пауза для отрицательного утверждения — единственного в этой спеке.
 *
 * Отсутствие события доказать нечем, кроме времени: «на A ничего не изменилось»
 * проверяется паузой. Поэтому рядом стоит **положительная** половина того же
 * утверждения — ответ сервера с прежним числом и то же число в REST, — иначе
 * зелёное здесь означало бы и «не изменилось», и «не смотрели».
 */
const QUIET_MS = 3_000

interface Row {
	id: string
	seq: number
	state: string | null
}

/** Что приёмка читает из production-разметки. */
interface Surface {
	state: string | null
	boundary: number | null
	myRead: number | null
	peerRead: number | null
	presence: string | null
	unread: number | null
	rows: Row[]
}

interface Sent {
	message_id: string
	seq: number
}

/** Беседа как её отдаёт REST — только те два поля, которыми сверяется клиент. */
interface RestConversation {
	unread: number | null
	readStates: Array<{ userId: string; readSeq: number }>
}

async function readSurface(page: Page, conversationId: string): Promise<Surface> {
	return page.evaluate((id) => {
		// Пустая строка и отсутствие атрибута — одно и то же «сервер не сказал»:
		// `undefined` в разметке атрибута не создаёт, а ноль остаётся нулём.
		const number = (value: string | null | undefined): number | null =>
			value === null || value === undefined || value === "" ? null : Number(value)

		const pane = document.querySelector("[data-connection-state]")
		const list = document.querySelector("[data-applied-through-seq]")
		const row = document.querySelector(`[data-conversation-id="${id}"]`)
		const header = document.querySelector("[data-presence]")
		const nodes = Array.from(document.querySelectorAll("[data-message-id]"))

		return {
			state: pane?.getAttribute("data-connection-state") ?? null,
			boundary: number(list?.getAttribute("data-applied-through-seq")),
			myRead: number(pane?.getAttribute("data-my-read-seq")),
			peerRead: number(pane?.getAttribute("data-peer-read-seq")),
			presence: header?.getAttribute("data-presence") ?? null,
			unread: number(row?.getAttribute("data-unread-count")),
			rows: nodes.map((node) => ({
				id: node.getAttribute("data-message-id") ?? "",
				seq: Number(node.getAttribute("data-message-seq")),
				state: node.getAttribute("data-message-state"),
			})),
		}
	}, conversationId)
}

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
	// обязан сказать это здесь, а не разбираться с последствиями в сверке ленты.
	expect(
		[200, 201],
		`POST /messages («${text}») — сообщение обязано быть принято`,
	).toContain(response.status())

	const body = (await response.json()) as { message_id?: unknown; seq?: unknown }
	expect(typeof body.message_id, "ответ отправки несёт message_id").toBe("string")
	expect(typeof body.seq, "ответ отправки несёт seq").toBe("number")

	return { message_id: body.message_id as string, seq: body.seq as number }
}

/** Квитанция устройства — ровно та операция, что объявлена контрактом. */
async function receipts(
	page: Page,
	token: string,
	fixture: Fixture,
	conversationId: string,
	body: { delivered_seq?: number; read_seq?: number },
): Promise<{ delivered_seq: number; read_seq: number }> {
	const response = await page.request.post(`${API}/conversations/${conversationId}/receipts`, {
		headers: headers(fixture, token),
		data: body,
	})

	expect(
		[200, 201],
		`POST /receipts (${JSON.stringify(body)}) — квитанция обязана быть принята`,
	).toContain(response.status())

	// Ответ несёт состояние **после** записи, а не присланное: этим и проверяется
	// монотонность: отставшая квитанция получает в ответ прежнее число.
	const state = (await response.json()) as { delivered_seq?: unknown; read_seq?: unknown }
	expect(typeof state.delivered_seq, "ответ квитанции несёт delivered_seq").toBe("number")
	expect(typeof state.read_seq, "ответ квитанции несёт read_seq").toBe("number")

	return { delivered_seq: state.delivered_seq as number, read_seq: state.read_seq as number }
}

/** Список бесед — источник истины для счётчика непрочитанного и состояния чтения. */
async function listFromRest(
	page: Page,
	token: string,
	fixture: Fixture,
	conversationId: string,
): Promise<RestConversation> {
	const response = await page.request.get(`${API}/conversations`, {
		headers: headers(fixture, token),
	})
	expect(response.status(), "GET /conversations — список бесед доступен").toBe(200)

	const body = (await response.json()) as {
		items?: Array<{
			conversation_id?: unknown
			unread_count?: unknown
			read_states?: Array<{
				user_id?: unknown
				last_read_seq?: unknown
			}>
		}>
	}

	const item = (body.items ?? []).find((entry) => entry.conversation_id === conversationId)
	if (item === undefined) {
		throw new Error(`беседа ${conversationId} отсутствует в ответе GET /conversations`)
	}

	return {
		unread: typeof item.unread_count === "number" ? item.unread_count : null,
		readStates: (item.read_states ?? []).map((state) => ({
			userId: String(state.user_id),
			readSeq: Number(state.last_read_seq),
		})),
	}
}

/** Хвост истории из REST — порядок, с которым сверяется порядок ленты в DOM. */
async function history(
	page: Page,
	token: string,
	fixture: Fixture,
	conversationId: string,
	afterSeq: number,
	limit: number,
): Promise<number[]> {
	const query = new URLSearchParams({ after_seq: String(afterSeq), limit: String(limit) })
	const response = await page.request.get(
		`${API}/conversations/${conversationId}/messages?${query.toString()}`,
		{ headers: headers(fixture, token) },
	)
	expect(response.status(), "GET /messages — история доступна").toBe(200)

	const body = (await response.json()) as { items?: Array<{ seq?: unknown }> }
	return (body.items ?? []).map((message) => Number(message.seq))
}

/** Открывает беседу и доводит панель до наблюдаемого состояния. */
async function openConversation(
	page: Page,
	conversationId: string,
	who: string,
): Promise<void> {
	await page.locator(`[data-conversation-id="${conversationId}"]`).click()

	await expect(
		page.locator("[data-connection-state]"),
		`${who}: панель беседы подняла соединение — без него публикациям некуда доехать`,
	).toHaveAttribute("data-connection-state", "connected", { timeout: CONNECT_TIMEOUT })

	await expect(
		page.locator("[data-applied-through-seq]"),
		`${who}: снимок хвоста применён — граница названа`,
	).toHaveAttribute("data-applied-through-seq", /^\d+$/, { timeout: CONNECT_TIMEOUT })
}

/**
 * Вторая вкладка того же человека: страница **того же** контекста.
 *
 * Своего контекста у неё нет намеренно: `addInitScript` кладёт `device_id`
 * контексту, и вторая страница обязана встать на то же устройство — иначе это
 * было бы второе устройство, а не вторая вкладка, и `RCP-002` проверялся бы на
 * другом предмете.
 *
 * Навигации здесь **нет** и быть не должно: её делает `signInTab`, потому что
 * ожидание обмена ставится до неё. `goto` на чистой странице — часть входа, а не
 * подготовка к нему; вынеси его наружу — и обмен мог бы пройти раньше, чем
 * подписка на ответ встанет, а ожидание поймало бы следующий обмен страницы
 * (обновление токена по таймеру) и вернуло бы чужой токен.
 */
async function secondTab(signedIn: SignedIn, fixture: Fixture): Promise<SignedIn> {
	const page = await signedIn.context.newPage()

	// Путь входа второй вкладки — **утверждение**, а не подробность. Пойди она
	// своим входом через удостоверяющий центр — это была бы вторая сессия и второе
	// устройство, то есть уже не «одна сессия живёт сразу в нескольких вкладках»
	// (`0006_realtime_connections.sql`), и `RCP-002` проверялся бы на другом
	// предмете. Поэтому путь фиксируется слушателем до навигации: у вкладки того же
	// контекста cookie обновления живой, и запросов к удостоверяющему центру не
	// должно быть **ни одного**.
	const toIdp: string[] = []
	page.on("request", (request) => {
		if (request.url().startsWith(IDP_ORIGIN)) toIdp.push(request.url())
	})

	const token = await signInTab(page, fixture)

	expect(
		toIdp,
		"вторая вкладка входит обменом cookie, а не вторым входом: запросов к удостоверяющему центру нет",
	).toEqual([])

	return { context: signedIn.context, page, token }
}

/** Страницы всех участников сценария — с именами, чтобы отказ называл виновника. */
type Side = readonly [string, Page]

test("8а: две вкладки одного человека отправляют одновременно — лента сходится", async ({
	browser,
}) => {
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
		const b2 = await secondTab(b, B)
		const sides: Side[] = [
			["A", a.page],
			["B1", b.page],
			["B2", b2.page],
		]

		await test.step("вкладки уложены: обе страницы одного контекста живы одновременно", async () => {
			await openConversation(a.page, conversationId, "A")
			await openConversation(b.page, conversationId, "B1")
			await openConversation(b2.page, conversationId, "B2")

			// Вторая вкладка вошла уже **после** того, как первая подняла соединение,
			// поэтому проверка здесь — не повтор: она утверждает, что вторая не сняла
			// первую. Без неё «две вкладки» означали бы «одна живая и одна
			// отвалившаяся», и сходимость доказывалась бы на одной.
			await expect(
				b.page.locator("[data-connection-state]"),
				"B1 остаётся живой после входа второй вкладки",
			).toHaveAttribute("data-connection-state", "connected")

			// Сессия жива не только по атрибуту: запрос с удостоверением первой
			// вкладки продолжает отвечать. Атрибут состояния мог бы остаться
			// `connected` и у снятой сессии — запрос этого не позволит.
			const alive = await b.page.request.get(`${API}/me`, { headers: headers(B, b.token) })
			expect(alive.status(), "запрос первой вкладки продолжает отвечать 200").toBe(200)

			const states = []
			for (const [who, page] of sides) {
				const surface = await readSurface(page, conversationId)
				states.push(`${who}=${String(surface.state)}`)
			}

			console.log(
				`  G3-007 (8а): укладка — один контекст, три страницы (B2 вошла обменом ` +
					`cookie на токен, к удостоверяющему центру не ходила); состояния ${states.join(", ")}`,
			)
		})

		const sent = await test.step("обе вкладки отправляют одновременно", async () => {
			const [first, second] = await Promise.all([
				send(b.page, b.token, B, conversationId, "одновременно: первая вкладка"),
				send(b2.page, b2.token, B, conversationId, "одновременно: вторая вкладка"),
			])

			expect(
				first.seq,
				"одновременные отправки заняли разные номера — иначе сравнивать было бы нечего",
			).not.toBe(second.seq)

			console.log(
				`  G3-007 (8а): одновременная отправка — seq ${first.seq} (B1) и ${second.seq} (B2)`,
			)
			return [first, second] as const
		})

		await test.step("лента сходится на трёх страницах и с порядком из REST", async () => {
			const head = Math.max(sent[0].seq, sent[1].seq)

			// Сходимость начинается с границы: пока страница не применила голову,
			// сравнивать списки рано, и «одинаково неполные» прошли бы как согласие.
			for (const [who, page] of sides) {
				await expect(
					page.locator("[data-applied-through-seq]"),
					`${who}: граница применённого дошла до головы обеих отправок`,
				).toHaveAttribute("data-applied-through-seq", String(head), {
					timeout: PUBLICATION_TIMEOUT,
				})
			}

			const surfaces = new Map<string, Surface>()
			for (const [who, page] of sides) {
				surfaces.set(who, await readSurface(page, conversationId))
			}

			const reference = surfaces.get("A") as Surface
			expect(reference.rows.length, "лента не пуста").toBeGreaterThan(0)
			expect(
				new Set(reference.rows.map((row) => row.id)).size,
				"идентификаторы уникальны во всей ленте",
			).toBe(reference.rows.length)

			const seqsOf = (surface: Surface): number[] => surface.rows.map((row) => row.seq)

			for (let index = 1; index < reference.rows.length; index += 1) {
				expect(
					reference.rows[index].seq,
					`порядок ленты на позиции ${index}`,
				).toBeGreaterThan(reference.rows[index - 1].seq)
			}

			// Шаг ровно `+1` — это не про монотонность, а про отсутствие дыр: лента
			// с пропущенным сообщением возрастала бы точно так же.
			const tail = seqsOf(reference)
			expect(
				tail,
				"в возрастании нет дыр: каждый следующий номер ровно на единицу больше",
			).toEqual(tail.map((_, index) => tail[0] + index))

			// Обе вкладки сравниваются с одной и той же лентой, а не друг с другом:
			// одинаково неверный порядок на обеих прошёл бы как сходимость.
			for (const who of ["B1", "B2"]) {
				expect(
					seqsOf(surfaces.get(who) as Surface),
					`порядок ленты вкладки ${who} совпадает с порядком у собеседника`,
				).toEqual(tail)
			}

			// И тот же порядок, прочитанный из REST: без него сходимость доказывала
			// бы лишь то, что две вкладки согласны между собой, а не с сервером.
			const fromRest = await history(
				a.page,
				a.token,
				A,
				conversationId,
				tail[0] - 1,
				tail.length,
			)
			expect(
				fromRest,
				"порядок ленты в окне совпадает с порядком, который отдаёт REST",
			).toEqual(tail)

			// Оба отправленных сообщения опознаются по `message_id` из ответов
			// отправки, а не по тексту: текст мог бы совпасть случайно.
			for (const message of sent) {
				const row = reference.rows.find((entry) => entry.id === message.message_id)
				expect(
					row?.seq,
					`сообщение ${message.message_id} лежит в ленте со своим seq`,
				).toBe(message.seq)
			}

			console.log(
				`  G3-007 (8а): лента — ${tail.length} сообщений, номера ${tail[0]} … ` +
					`${tail[tail.length - 1]}; совпали у A, B1, B2 и в REST`,
			)
		})
	} finally {
		for (const context of contexts) await context.close()
	}
})

test("8б: непрочитанное — событием и сверкой, вкладки не расходятся", async ({ browser }) => {
	const state = JSON.parse(readFileSync(STATE_FILE, "utf8")) as State
	const conversationId = state.conversationId

	const contexts: BrowserContext[] = []

	const open = async (fixture: Fixture): Promise<SignedIn> => {
		const signedIn = await signIn(browser, fixture)
		contexts.push(signedIn.context)
		return signedIn
	}

	try {
		const a = await open(A)
		const b = await open(B)
		const b2 = await secondTab(b, B)
		const contextB = b.context

		await openConversation(a.page, conversationId, "A")
		await openConversation(b.page, conversationId, "B1")
		await openConversation(b2.page, conversationId, "B2")

		const readOf = async (): Promise<{ b1: Surface; b2: Surface; rest: RestConversation }> => ({
			b1: await readSurface(b.page, conversationId),
			b2: await readSurface(b2.page, conversationId),
			rest: await listFromRest(a.page, a.token, A, conversationId),
		})

		const baseline = await test.step("счётчики сходятся до воздействия: вкладки и REST", async () => {
			// Лента обязана перерасти окно, и это **предварительное условие**, а не
			// подготовка для красоты: прижатая к низу лента не даёт вкладке
			// объявить меньше применённого, и «прочитано» уезжало бы вместе с
			// приходящими сообщениями. С `read < границы` новое сообщение встаёт
			// ниже сгиба, и объявленное прочтение остаётся на месте — без этого
			// `N + K` недостижимо вовсе, и утверждение было бы о другом.
			await until(
				async () => (await readSurface(b.page, conversationId)).myRead !== null,
				async () => {
					const surface = await readSurface(b.page, conversationId)
					return `вкладка B1: read=${String(surface.myRead)} граница=${String(surface.boundary)}`
				},
				"вкладка B сообщила первое прочтение",
				PUBLICATION_TIMEOUT,
			)

			let filled = 0
			for (let index = 0; index < FILL_CAP; index += 1) {
				const surface = await readSurface(b.page, conversationId)
				if (
					surface.myRead !== null &&
					surface.boundary !== null &&
					surface.myRead < surface.boundary
				) {
					break
				}
				await send(a.page, a.token, A, conversationId, `наполнение ${index + 1}`)
				filled += 1
			}

			const measured = await readSurface(b.page, conversationId)
			expect(
				measured.myRead !== null &&
					measured.boundary !== null &&
					measured.myRead < measured.boundary,
				`лента переросла окно: объявленное прочтение (${String(measured.myRead)}) ` +
					`строго ниже границы (${String(measured.boundary)}) — иначе счётчик ` +
					`уезжал бы вместе с сообщениями, и сценарий проверял бы не счёт`,
			).toBe(true)

			// До воздействия вкладки обязаны сойтись с REST. Это тоже условие, а не
			// украшение: расходись они **до** проверки, её зелёное ничего не значило бы.
			await until(
				async () => {
					const { b1, b2, rest } = await readOf()
					return (
						b1.unread !== null && b2.unread !== null && b1.unread === b2.unread && b1.unread === rest.unread
					)
				},
				async () => {
					const { b1, b2, rest } = await readOf()
					return `B1=${String(b1.unread)} B2=${String(b2.unread)} REST=${String(rest.unread)}`
				},
				"счётчики вкладок и REST сходятся до воздействия",
				PUBLICATION_TIMEOUT,
			)

			const { b1 } = await readOf()
			const n = b1.unread as number
			expect(n, "непрочитанное измерено, а не сочинено: оно не ноль").toBeGreaterThan(0)

			// Прочтение уносится из шага, чтобы следующее утверждение сравнивало с ним,
			// а не с самим собой: «не двигалось» без прежнего числа — не утверждение.
			const read = b1.myRead as number

			console.log(
				`  G3-007 (8б): наполнение — ${filled} сообщений, объявленное прочтение ` +
					`${String(read)}, граница ${String(b1.boundary)}, N = ${n} ` +
					`(B1, B2 и REST сходятся)`,
			)
			return { n, read }
		})

		const arrived = await test.step(`A отправляет ${ARRIVED} сообщения: событие несёт абсолютное число`, async () => {
			const sent = []
			for (let index = 0; index < ARRIVED; index += 1) {
				sent.push(await send(a.page, a.token, A, conversationId, `чужое ${index + 1}`))
			}

			const expected = baseline.n + ARRIVED

			// Абсолютное значение, а не «одинаково»: равенство двух устаревших
			// счётчиков истинно и при полностью мёртвом личном канале — именно это
			// делало бы проверку недоказывающей.
			for (const [who, page] of [["B1", b.page], ["B2", b2.page]] as const) {
				await expect(
					page.locator(`[data-conversation-id="${conversationId}"]`),
					`${who}: непрочитанное выросло ровно на пришедшее, и это то же число, ` +
						`что у второй вкладки, — а не то же молчание`,
				).toHaveAttribute("data-unread-count", String(expected), {
					timeout: PUBLICATION_TIMEOUT,
				})
			}

			// Третье представление того же числа — источник истины (D11): оно сверяется
			// с REST, а не со второй вкладкой.
			const fromRest = await listFromRest(a.page, a.token, A, conversationId)
			expect(
				fromRest.unread,
				`непрочитанное в REST равно ${expected} — счётчик вкладки сверен с источником, ` +
					`а не с другим оверлеем`,
			).toBe(expected)

			// Прочтение за это время не двигалось: счётчик вырос от прихода, а не от
			// того, что вкладка объявила прочитанным пришедшее. Сравнение идёт с
			// числом, измеренным **до** воздействия: пришедшее ушло ниже сгиба, значит
			// взгляда на него не было, и объявленное прочтение обязано стоять.
			const after = await readSurface(b.page, conversationId)
			expect(
				after.myRead,
				"объявленное прочтение не двигалось: наполнение ушло ниже сгиба, взгляда не было",
			).toBe(baseline.read)

			console.log(
				`  G3-007 (8б): событием — N = ${baseline.n}, доставлено ${ARRIVED}, ` +
					`ожидание ${expected}, REST ${String(fromRest.unread)}, seq ` +
					`${sent.map((message) => message.seq).join(", ")}; ` +
					`прочтение ${baseline.read} → ${String(after.myRead)}`,
			)
			return expected
		})

		await test.step("предмет уходит в офлайн, и это наблюдаемо", async () => {
			await contextB.setOffline(true)

			await expect(
				b.page.locator("[data-connection-state]"),
				"DISCONNECTED от браузерного события offline: уход обязан быть наблюдаемым, " +
					"иначе шаг молча пропущен, и сверка доказывала бы пустоту",
			).toHaveAttribute("data-connection-state", "disconnected", { timeout: OFFLINE_TIMEOUT })
		})

		const missed = await test.step(`${MISSED} сообщения проходят мимо вкладок, но не мимо REST`, async () => {
			for (let index = 0; index < MISSED; index += 1) {
				await send(a.page, a.token, A, conversationId, `мимо ${index + 1}`)
			}

			const expected = arrived + MISSED

			// Вкладки стоят на прежнем числе — событий не было, потому что канала не
			// было. Это и есть расхождение, которое обязана закрыть сверка.
			for (const [who, page] of [["B1", b.page], ["B2", b2.page]] as const) {
				await expect(
					page.locator(`[data-conversation-id="${conversationId}"]`),
					`${who}: оставшись без канала, вкладка держит прежнее число`,
				).toHaveAttribute("data-unread-count", String(arrived))
			}

			await until(
				async () => (await listFromRest(a.page, a.token, A, conversationId)).unread === expected,
				async () =>
					`REST: ${String((await listFromRest(a.page, a.token, A, conversationId)).unread)} (ждём ${expected})`,
				"источник истины разошёлся с вкладками",
				PUBLICATION_TIMEOUT,
			)

			console.log(
				`  G3-007 (8б): сверка — вкладки держат ${arrived}, REST несёт ${expected}: ` +
					`расхождение предъявлено, а не описано`,
			)
			return expected
		})

		await test.step("возврат связи: клиент сам идёт за истиной и оверлей заменяется", async () => {
			// Сверка при возврате (D11) наблюдается **механизмом**, а не только числом:
			// регистрация ожидания идёт до возврата сети, поэтому увиденный ответ
			// списка бесед — это собственный поход клиента за истиной, а не совпадение.
			const reconcile = b.page.waitForResponse(
				(response) =>
					response.request().method() === "GET" &&
					new URL(response.url()).pathname.endsWith("/conversations"),
				{ timeout: PUBLICATION_TIMEOUT },
			)

			await contextB.setOffline(false)
			await reconcile

			// Оверлей заменяется ответом, а не правится относительно прежнего: обе
			// вкладки обязаны прийти к тому же числу, что лежит в REST.
			for (const [who, page] of [["B1", b.page], ["B2", b2.page]] as const) {
				await expect(
					page.locator(`[data-conversation-id="${conversationId}"]`),
					`${who}: после возврата связи число взято из REST — расхождение закрыто`,
				).toHaveAttribute("data-unread-count", String(missed), {
					timeout: PUBLICATION_TIMEOUT,
				})
			}

			console.log(
				`  G3-007 (8б): возврат — обе вкладки на ${missed}, совпадает с REST; ` +
					`ответ списка бесед наблюдён как собственный запрос клиента`,
			)
		})
	} finally {
		for (const context of contexts) await context.close()
	}
})

test("присутствие: предикат времени, а не состояние сокета", async ({ browser }) => {
	const state = JSON.parse(readFileSync(STATE_FILE, "utf8")) as State
	const conversationId = state.conversationId

	const contexts: BrowserContext[] = []

	const open = async (fixture: Fixture): Promise<SignedIn> => {
		const signedIn = await signIn(browser, fixture)
		contexts.push(signedIn.context)
		return signedIn
	}

	try {
		const a = await open(A)
		const b = await open(B)
		const contextB = b.context

		const observed = await test.step("собеседник подключён: шапка несёт online", async () => {
			await openConversation(a.page, conversationId, "A")
			await openConversation(b.page, conversationId, "B")

			await expect(
				a.page.locator("[data-presence]"),
				"присутствие собеседника видно читателю: сервер считает его из отметки активности",
			).toHaveAttribute("data-presence", "online", { timeout: PUBLICATION_TIMEOUT })

			return (await readSurface(a.page, conversationId)).presence
		})

		await test.step("собеседник закрыт, но ответ REST всё ещё online", async () => {
			// Сокет закрыт — значит присутствие, если бы оно было состоянием сокета,
			// обязано было бы пропасть. Оно не пропадает: сервер считает его
			// предикатом по `last_seen_at` с окном 180 секунд (`domain/presence.py`),
			// и это ровно то, что отличает D4 от «слушаем события присутствия».
			await contextB.close()

			// Страница перезагружается и входит заново — тем же контекстом, тем же
			// устройством: токен живёт в памяти страницы, и после перезагрузки его
			// надо добыть снова, а не выдать из проверки. Перезагружает её сам вход:
			// `signInTab` навигирует, потому что ожидание обмена ставится до навигации
			// (иначе обмен прошёл бы до подписки и остался незамеченным). Контекст уже
			// вошёл, cookie на месте, токен в памяти страницы стёрт навигацией — значит
			// здесь работает **обмен**, та же ветка, что у второй вкладки, а не кнопка.
			await signInTab(a.page, A)
			await openConversation(a.page, conversationId, "A")

			await expect(
				a.page.locator("[data-presence]"),
				"после закрытия сокета собеседника присутствие остаётся: это предикат времени",
			).toHaveAttribute("data-presence", "online", { timeout: PUBLICATION_TIMEOUT })

			const redrawn = (await readSurface(a.page, conversationId)).presence
			console.log(
				`  G3-007 (присутствие): до закрытия сокета — ${String(observed)}, ` +
					`после закрытия и перезагрузки — ${String(redrawn)}; окно 180 с, ` +
					`online=false браузером не предъявляется (доказан юнитом)`,
			)
		})
	} finally {
		for (const context of contexts) await context.close()
	}
})

test("9: квитанция доезжает, идёт только вперёд и читается из REST", async ({ browser }) => {
	const state = JSON.parse(readFileSync(STATE_FILE, "utf8")) as State
	const conversationId = state.conversationId

	const contexts: BrowserContext[] = []

	const open = async (fixture: Fixture): Promise<SignedIn> => {
		const signedIn = await signIn(browser, fixture)
		contexts.push(signedIn.context)
		return signedIn
	}

	try {
		const a = await open(A)
		const b = await open(B)
		const b2 = await secondTab(b, B)
		const contextA = a.context

		await openConversation(a.page, conversationId, "A")
		await openConversation(b.page, conversationId, "B1")
		await openConversation(b2.page, conversationId, "B2")

		const mine = await test.step("своё сообщение занимает номер S и лежит у обоих", async () => {
			const probe = await send(a.page, a.token, A, conversationId, "квитанция: наполнение")

			await until(
				async () => (await readSurface(b.page, conversationId)).boundary === probe.seq,
				async () => {
					const surface = await readSurface(b.page, conversationId)
					return `граница B1 ${String(surface.boundary)} (ждём ${probe.seq})`
				},
				"собеседник применил сообщение",
				PUBLICATION_TIMEOUT,
			)

			const surface = await readSurface(a.page, conversationId)
			const row = surface.rows.find((entry) => entry.id === probe.message_id)
			expect(row, "своё сообщение лежит в ленте автора").toBeDefined()
			expect(row?.state, "состояние своего сообщения названо разметкой").not.toBeNull()

			console.log(`  G3-007 (9): S = ${probe.seq}, состояние строки у A = ${String(row?.state)}`)
			return probe
		})

		await test.step("вкладка собеседника сообщает доставку сама, без запроса проверки", async () => {
			// Доставка — свойство устройства: публикация доходит и применяется без
			// всякого взгляда, поэтому вкладка обязана сообщить о ней сама. Состояние
			// своего сообщения у автора читается ровно отсюда, и его отличие от
			// `sent` и есть «квитанция доехала».
			await until(
				async () => {
					const surface = await readSurface(a.page, conversationId)
					const row = surface.rows.find((entry) => entry.id === mine.message_id)
					return row !== undefined && row.state !== null && row.state !== "sent"
				},
				async () => {
					const surface = await readSurface(a.page, conversationId)
					const row = surface.rows.find((entry) => entry.id === mine.message_id)
					return `состояние строки ${mine.message_id}: ${String(row?.state)}`
				},
				"доставка сообщения сообщена собеседником",
				PUBLICATION_TIMEOUT,
			)

			const observed = await readSurface(a.page, conversationId)
			const row = observed.rows.find((entry) => entry.id === mine.message_id)
			console.log(
				`  G3-007 (9): квитанция вкладки B доехала событием — состояние у A ` +
					`${String(row?.state)} (не sent), data-peer-read-seq = ${String(observed.peerRead)}`,
			)
		})

		await test.step("прочтение приезжает: номер на A равен S, и он не идёт назад", async () => {
			const accepted = await receipts(b2.page, b2.token, B, conversationId, {
				read_seq: mine.seq,
			})
			expect(accepted.read_seq, "сервер подтверждает принятое прочтение").toBe(mine.seq)

			await expect(
				a.page.locator("[data-connection-state]"),
				"номер прочтения собеседника виден автору",
			).toHaveAttribute("data-peer-read-seq", String(mine.seq), {
				timeout: PUBLICATION_TIMEOUT,
			})

			const surface = await readSurface(a.page, conversationId)
			const own = surface.rows.filter((row) => row.state !== null && row.seq <= mine.seq)
			expect(own.length, "свои сообщения до S в ленте есть — есть что проверять").toBeGreaterThan(0)
			expect(
				own.map((row) => row.state),
				"каждое своё сообщение до S отмечено прочитанным",
			).toEqual(own.map(() => "read"))

			// Отставшая квитанция: устройство, не знавшее о большем номере, сообщает
			// меньшее. Хранимое число не двигается, и в ответе — прежнее, а не присланное.
			const stale = await receipts(b.page, b.token, B, conversationId, {
				read_seq: mine.seq - 1,
			})
			expect(
				stale.read_seq,
				"ответ на отставшую квитанцию несёт прежнее число, а не присланное",
			).toBe(mine.seq)

			// Отрицательная половина: на A за это время ничего не изменилось. Рядом —
			// положительная: число осталось тем же и в REST.
			await a.page.waitForTimeout(QUIET_MS)

			const after = await readSurface(a.page, conversationId)
			expect(after.peerRead, "отставшая квитанция не откатила номер на A").toBe(mine.seq)
			expect(
				after.rows.filter((row) => row.state !== null && row.seq <= mine.seq).map((row) => row.state),
				"отметки прочтения остались на месте",
			).toEqual(own.map(() => "read"))

			const rest = await listFromRest(a.page, a.token, A, conversationId)
			const peer = rest.readStates.find((entry) => entry.userId === state.userIds.b)
			expect(peer?.readSeq, "REST по-прежнему несёт большее число").toBe(mine.seq)

			console.log(
				`  G3-007 (9): квитанция S = ${mine.seq} — на A data-peer-read-seq ` +
					`${String(surface.peerRead)} → ${String(after.peerRead)} после отставшей ` +
					`(S-1); в REST ${String(peer?.readSeq)}`,
			)
		})

		await test.step("истина из REST: номер, пришедший, пока автор был офлайн", async () => {
			// Следующее своё сообщение уходит **до** ухода в офлайн: его номер станет
			// тем, о прочтении которого A узнает, не увидев ни одного события.
			const later = await send(a.page, a.token, A, conversationId, "квитанция: после пробы")
			expect(later.seq, "новое сообщение занимает номер за S").toBeGreaterThan(mine.seq)

			const before = await readSurface(a.page, conversationId)
			const row = before.rows.find((entry) => entry.id === later.message_id)
			expect(
				row?.state,
				"до прочтения состояние не read: иначе наблюдать было бы нечего",
			).not.toBe("read")

			await contextA.setOffline(true)
			await expect(
				a.page.locator("[data-connection-state]"),
				"DISCONNECTED от браузерного события offline",
			).toHaveAttribute("data-connection-state", "disconnected", { timeout: OFFLINE_TIMEOUT })

			// Пока автор офлайн, собеседник читает — и событие о прочтении до автора
			// не доходит вовсе: канала нет.
			const away = await receipts(b2.page, b2.token, B, conversationId, {
				read_seq: later.seq,
			})
			expect(away.read_seq, "прочтение принято сервером, пока автор был офлайн").toBe(later.seq)

			const missed = await readSurface(a.page, conversationId)
			expect(
				missed.peerRead,
				"пока канала не было, номер у автора не двигался",
			).toBe(mine.seq)

			const reconcile = a.page.waitForResponse(
				(response) =>
					response.request().method() === "GET" &&
					new URL(response.url()).pathname.endsWith("/conversations"),
				{ timeout: PUBLICATION_TIMEOUT },
			)

			await contextA.setOffline(false)
			await reconcile

			await expect(
				a.page.locator("[data-connection-state]"),
				"номер собеседника доехал до автора, которого не было в канале",
			).toHaveAttribute("data-peer-read-seq", String(later.seq), {
				timeout: PUBLICATION_TIMEOUT,
			})

			const after = await readSurface(a.page, conversationId)
			const redrawn = after.rows.find((entry) => entry.id === later.message_id)
			expect(
				redrawn?.state,
				"своё сообщение отмечено прочитанным без единого события у автора",
			).toBe("read")

			// Различить пути здесь нечем и не нужно: восстановление подписки может
			// принести то же событие, что и сверка, и оба ведут к одному числу. Что
			// наблюдается **отдельно** — что клиент пошёл за истиной сам: ответ списка
			// бесед пойман выше, и ожидание стояло до возврата сети.
			const rest = await listFromRest(a.page, a.token, A, conversationId)
			const peer = rest.readStates.find((entry) => entry.userId === state.userIds.b)
			expect(peer?.readSeq, "и то же число лежит в REST").toBe(later.seq)

			console.log(
				`  G3-007 (9): сверка — пока A был офлайн, прочтение выросло с ${mine.seq} до ` +
					`${later.seq}; у A ${String(missed.peerRead)} → ${String(after.peerRead)}, ` +
					`в REST ${String(peer?.readSeq)}`,
			)
		})
	} finally {
		for (const context of contexts) await context.close()
	}
})
