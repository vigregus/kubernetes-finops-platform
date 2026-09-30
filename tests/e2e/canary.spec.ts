import { randomUUID } from "node:crypto"
import { expect, test } from "@playwright/test"
import type { BrowserContext, Page } from "@playwright/test"
import { API, headers, signIn } from "./support/auth"
import type { Fixture, SignedIn } from "./support/auth"

/**
 * Раздел 16 плана load-testing (docs/messenger/15-load-testing-platform.md)
 * — небольшой browser canary параллельно k6 workload: не тысячи Chromium,
 * 1–5 пар, каждая периодически шлёт уникальный текст и проверяет, что он
 * доходит до партнёра без перезагрузки, а потом получает ответ тем же
 * путём. Не нагрузочный профиль (throughput меряет k6), а живой свидетель
 * того, что реальный браузер с реальным Centrifugo-соединением видит
 * сообщения в момент прогона — то, чего ни k6 (HTTP), ни reconciliation
 * (Postgres) не проверяют.
 *
 * Пары берутся из уже готовых synthetic-пользователей и бесед LOCAL-
 * CAPACITY-001 (provision + prepare-conversations, docs — раздел 8): та же
 * пара `(2i-1, 2i)`, для которой prepare-conversations уже завёл беседу —
 * значит canary не создаёт собственных учётных записей и не нуждается в
 * отдельной уборке. Вход — тот же настоящий authorization-code через
 * support/auth.ts (signIn), которым уже проверены G3-005/006/007: второй
 * копии логики входа в этом репозитории быть не должно.
 *
 * Отправка — page.request.post, тем же путём, что в G3-006 (`send`):
 * не имитация клика по MessageComposer, а прямой вызов от лица браузерной
 * вкладки с её собственными cookie/token — тот же принцип, что уже
 * закреплён в g3-006-web.spec.ts. Наблюдение — data-message-id и
 * data-connection-state, те же атрибуты, что G3-006 использует для
 * доказательства живости WS-канала без перезагрузки.
 */

const RUN_ID = process.env.RUN_ID
const RUN_PASSWORD = process.env.RUN_PASSWORD
const PAIRS = Math.min(5, Math.max(1, parseInt(process.env.CANARY_PAIRS || "1", 10)))
const DURATION_SECONDS = parseInt(process.env.CANARY_DURATION_SECONDS || "60", 10)
const TICK_SECONDS = parseInt(process.env.CANARY_TICK_SECONDS || "10", 10)
/**
 * Отступ перед первым входом — даём k6-load пережить холодный старт своего
 * Job/раннера, прежде чем canary впервые обменивает код на токен.
 *
 * `k6-load` и `browser-canary` оба стартуют по `dependencies:
 * [playwright-smoke]` (раздел 16 — параллельно намеренно), но у k6 холодный
 * старт (создание Job, планирование пода, разгон VU) не мгновенный, а у
 * canary — свой (`npm ci` перед первым тестом). Живой дефект (верификация
 * §17, `local-capacity-mixed-rvq55`): первый же обмен кода на токен словил
 * 500 именно в этом стартовом окне — ответ не от `api` (в его access-логе
 * нет ни одного 500 за то время), а от промежуточного слоя под нагрузкой
 * чужого холодного старта. Число ниже — не измеренный порог, а бюджет с
 * запасом под этот класс гонки; `signIn` вдобавок повторяет попытку на
 * 5xx (`support/auth.ts`), так что отступ снижает частоту, а не заменяет
 * повтор.
 */
const START_DELAY_SECONDS = parseInt(process.env.CANARY_START_DELAY_SECONDS || "20", 10)

function requiredCanaryEnv(name: string, value: string | undefined): string {
	if (!value) {
		throw new Error(`${name} не задан: canary запускается только внутри Argo-шага browser-canary`)
	}
	return value
}

function pairFixture(index: number): Fixture {
	const email = `local-capacity-${requiredCanaryEnv("RUN_ID", RUN_ID)}-${String(index).padStart(6, "0")}@finops.local`
	return {
		email,
		password: requiredCanaryEnv("RUN_PASSWORD", RUN_PASSWORD),
		deviceId: randomUUID(),
	}
}

interface Sample {
	sendToVisibleMs: number
	failed: boolean
	reconnected: boolean
}

async function openConversation(signedIn: SignedIn, conversationId: string): Promise<void> {
	await signedIn.page.locator(`[data-conversation-id="${conversationId}"]`).click()
	await expect(
		signedIn.page.locator("[data-connection-state]"),
		"панель беседы несёт data-connection-state",
	).toHaveAttribute("data-connection-state", "connected", { timeout: 60_000 })
}

async function connectionState(page: Page): Promise<string | null> {
	return page.locator("[data-connection-state]").getAttribute("data-connection-state")
}

/** Одно направление обмена: sender шлёт, receiver видит без перезагрузки. */
async function roundTrip(
	sender: SignedIn,
	senderFixture: Fixture,
	receiver: SignedIn,
	conversationId: string,
	text: string,
): Promise<{ ms: number; ok: boolean }> {
	const start = Date.now()
	const response = await sender.page.request.post(`${API}/conversations/${conversationId}/messages`, {
		headers: headers(senderFixture, sender.token),
		data: { client_message_id: randomUUID(), type: "text", payload: { text } },
	})
	if (response.status() !== 200 && response.status() !== 201) {
		return { ms: Date.now() - start, ok: false }
	}
	const body = (await response.json()) as { message_id?: unknown }
	if (typeof body.message_id !== "string") {
		return { ms: Date.now() - start, ok: false }
	}
	try {
		await expect(
			receiver.page.locator(`[data-message-id="${body.message_id}"]`),
			"партнёр видит canary-сообщение без перезагрузки",
		).toHaveCount(1, { timeout: 30_000 })
	} catch {
		return { ms: Date.now() - start, ok: false }
	}
	return { ms: Date.now() - start, ok: true }
}

test("browser canary: периодический обмен уникальным текстом параллельно k6", async ({ browser }) => {
	const contexts: BrowserContext[] = []
	const samples: Sample[] = []

	const open = async (fixture: Fixture): Promise<SignedIn> => {
		const signedIn = await signIn(browser, fixture)
		contexts.push(signedIn.context)
		return signedIn
	}

	try {
		await wait(START_DELAY_SECONDS * 1000)

		for (let pair = 0; pair < PAIRS; pair += 1) {
			const indexA = pair * 2 + 1
			const indexB = pair * 2 + 2
			const fixtureA = pairFixture(indexA)
			const fixtureB = pairFixture(indexB)

			const a = await open(fixtureA)
			const b = await open(fixtureB)

			const conversations = await a.page.request.get(`${API}/conversations`, {
				headers: headers(fixtureA, a.token),
			})
			expect(conversations.status(), `GET /conversations для пары ${pair + 1}`).toBe(200)
			const body = (await conversations.json()) as { items?: Array<{ conversation_id?: unknown }> }
			const conversationId = body.items?.[0]?.conversation_id
			expect(
				typeof conversationId,
				`пара ${pair + 1}: у пользователя ${indexA} нет беседы — prepare-conversations не отработал`,
			).toBe("string")

			await openConversation(a, conversationId as string)
			await openConversation(b, conversationId as string)

			const deadline = Date.now() + DURATION_SECONDS * 1000
			let tick = 0
			while (Date.now() < deadline) {
				tick += 1
				const tickStart = Date.now()

				let reconnected = false
				for (const [side, signedIn] of [
					["A", a],
					["B", b],
				] as const) {
					const state = await connectionState(signedIn.page)
					if (state !== "connected") {
						reconnected = true
						await expect(
							signedIn.page.locator("[data-connection-state]"),
							`сторона ${side} пары ${pair + 1}: переподключение перед тиком ${tick}`,
						).toHaveAttribute("data-connection-state", "connected", { timeout: 30_000 })
					}
				}

				const forward = await roundTrip(
					a,
					fixtureA,
					b,
					conversationId as string,
					`canary ${RUN_ID} p${pair + 1} t${tick} A->B ${Date.now()}`,
				)
				samples.push({ sendToVisibleMs: forward.ms, failed: !forward.ok, reconnected })

				const backward = await roundTrip(
					b,
					fixtureB,
					a,
					conversationId as string,
					`canary ${RUN_ID} p${pair + 1} t${tick} B->A ${Date.now()}`,
				)
				samples.push({ sendToVisibleMs: backward.ms, failed: !backward.ok, reconnected: false })

				const elapsed = Date.now() - tickStart
				await wait(TICK_SECONDS * 1000 - elapsed)
			}
		}
	} finally {
		for (const context of contexts) await context.close()
	}

	const failures = samples.filter((s) => s.failed).length
	const reconnects = samples.filter((s) => s.reconnected).length
	const ok = samples.filter((s) => !s.failed).map((s) => s.sendToVisibleMs).sort((x, y) => x - y)
	const p95 = ok.length > 0 ? ok[Math.min(ok.length - 1, Math.floor(ok.length * 0.95))] / 1000 : 0

	console.log(
		`canary: ${samples.length} обменов, ${failures} отказов, ${reconnects} переподключений, ` +
			`send-to-visible p95=${p95.toFixed(3)}s (${PAIRS} пар, ${DURATION_SECONDS}s)`,
	)
	console.log(`CANARY_METRICS failures=${failures} reconnects=${reconnects} p95_seconds=${p95}`)

	expect(failures, "ни один canary-обмен не должен отказать").toBe(0)
})

async function wait(ms: number): Promise<void> {
	if (ms <= 0) return
	await new Promise((resolve) => setTimeout(resolve, ms))
}
