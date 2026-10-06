import { randomUUID } from "node:crypto"
import { expect, test } from "@playwright/test"
import type { Browser, BrowserContext } from "@playwright/test"
import { API, headers, signIn } from "./support/auth"
import type { Fixture, SignedIn } from "./support/auth"
import { packetsReceived, trackPeers } from "./support/calls"

/**
 * Раздел 31 плана load-testing (`docs/messenger/15-load-testing-platform.md`) —
 * настоящие звонки с **аудио и видео** параллельно k6-нагрузке.
 *
 * k6 нагружает сигнализацию (`media.js`), но не умеет WebRTC. Эта спека ведёт
 * живые звонки двух настоящих Chromium с фейковыми камерой и микрофоном:
 * каждый звонок идёт через API, Centrifugo и (на стенде) прямое соединение,
 * а **медиа считается идущим, только когда растёт счётчик принятых пакетов**,
 * а не когда состояние «connected». Это не пропускная способность (она
 * определяется k6), а свидетель: пока сервер под нагрузкой, настоящий звонок
 * соединяется за приемлемое время и несёт звук и картинку.
 *
 * Пары берутся с **конца** списка synthetic-пользователей (пара `PAIRS`,
 * `PAIRS-1`, …), а k6 `media.js` занимает начало (`1…CALL_PAIRS`): у человека
 * один живой звонок, и одна пара под двумя ведущими звонки дала бы `busy`.
 *
 * Звонки пар идут одновременно (`Promise.all`), а внутри пары — друг за другом
 * до конца окна. Одна пара — это два контекста Chromium, поэтому пар мало
 * (по умолчанию 2, не больше 4): здесь проверка, а не нагрузка (R7 в
 * `16-calls-estimate.md`: качество и ёмкость локально не измеряются).
 */

const RUN_ID = process.env.RUN_ID
const RUN_PASSWORD = process.env.RUN_PASSWORD
const USERS = parseInt(process.env.USERS || "10", 10)
const PAIRS_TOTAL = Math.max(1, Math.floor(USERS / 2))
const CALL_PAIRS = Math.min(PAIRS_TOTAL, parseInt(process.env.CALL_PAIRS || "5", 10))
const CANARY_PAIRS = Math.min(
	4,
	Math.max(1, parseInt(process.env.CANARY_CALL_PAIRS || "2", 10)),
	// Не заходить на пары, которые ведёт k6.
	Math.max(1, PAIRS_TOTAL - CALL_PAIRS),
)
const DURATION_SECONDS = parseInt(process.env.CANARY_DURATION_SECONDS || "60", 10)
const HOLD_SECONDS = parseInt(process.env.CANARY_CALL_HOLD_SECONDS || "8", 10)
const START_DELAY_SECONDS = parseInt(process.env.CANARY_START_DELAY_SECONDS || "20", 10)

function need(name: string, value: string | undefined): string {
	if (!value) throw new Error(`${name} не задан: canary запускается только внутри Argo-шага browser-canary`)
	return value
}

function fixture(index: number): Fixture {
	return {
		email: `local-capacity-${need("RUN_ID", RUN_ID)}-${String(index).padStart(6, "0")}@finops.local`,
		password: need("RUN_PASSWORD", RUN_PASSWORD),
		deviceId: randomUUID(),
	}
}

interface Sample {
	kind: "audio" | "video"
	setupMs: number
	active: boolean
	mediaFlowing: boolean
}

const PHASE = (who: SignedIn) => who.page.locator("[data-call-phase]")

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)))

async function openConversation(who: SignedIn, conversationId: string): Promise<void> {
	await who.page.locator(`[data-conversation-id="${conversationId}"]`).click()
	await expect(who.page.locator("[data-connection-state]")).toHaveAttribute(
		"data-connection-state",
		"connected",
		{ timeout: 60_000 },
	)
}

/** Звонок сводится в чистое состояние, что бы ни случилось в предыдущем. */
async function hangUp(...people: SignedIn[]): Promise<void> {
	for (const who of people) {
		const end = who.page.getByRole("button", { name: "End call" })
		if (await end.isVisible().catch(() => false)) await end.click().catch(() => undefined)
		const decline = who.page.getByRole("button", { name: "Decline call" })
		if (await decline.isVisible().catch(() => false)) await decline.click().catch(() => undefined)
	}
}

async function oneCall(a: SignedIn, b: SignedIn, kind: "audio" | "video"): Promise<Sample> {
	const started = Date.now()
	await a.page.getByRole("button", { name: kind === "audio" ? "Start voice call" : "Start video call" }).click()
	let active = false
	let mediaFlowing = false
	try {
		await b.page.getByRole("alertdialog", { name: "Incoming call" }).waitFor({ timeout: 20_000 })
		await b.page.getByRole("button", { name: "Accept call" }).click()
		await expect(PHASE(a)).toHaveAttribute("data-call-phase", "active", { timeout: 40_000 })
		await expect(PHASE(b)).toHaveAttribute("data-call-phase", "active", { timeout: 40_000 })
		active = true
		const setupMs = Date.now() - started
		const before = await packetsReceived(b.page)
		await wait(HOLD_SECONDS * 1000)
		mediaFlowing = (await packetsReceived(b.page)) > before
		await hangUp(a, b)
		return { kind, setupMs, active, mediaFlowing }
	} catch {
		await hangUp(a, b)
		return { kind, setupMs: Date.now() - started, active, mediaFlowing }
	}
}

async function runPair(browser: Browser, index: number, samples: Sample[], contexts: BrowserContext[]): Promise<void> {
	const pair = PAIRS_TOTAL - index
	const fixtureA = fixture(pair * 2 - 1)
	const a = await signIn(browser, fixtureA)
	const b = await signIn(browser, fixture(pair * 2))
	contexts.push(a.context, b.context)
	await trackPeers(a.context)
	await trackPeers(b.context)
	await a.page.reload()
	await b.page.reload()

	const response = await a.page.request.get(`${API}/conversations`, {
		headers: headers(fixtureA, a.token),
	})
	expect(response.status(), `GET /conversations для пары ${pair}`).toBe(200)
	const body = (await response.json()) as { items?: Array<{ conversation_id?: unknown }> }
	const conversationId = body.items?.[0]?.conversation_id
	expect(typeof conversationId, `пара ${pair}: нет беседы`).toBe("string")

	await openConversation(a, conversationId as string)
	await openConversation(b, conversationId as string)

	const deadline = Date.now() + DURATION_SECONDS * 1000
	let n = 0
	while (Date.now() < deadline) {
		const kind = n % 2 === 0 ? "audio" : "video"
		n += 1
		samples.push(await oneCall(a, b, kind))
		await wait(1000)
	}
}

function percentile(values: number[], p: number): number {
	if (values.length === 0) return 0
	const sorted = [...values].sort((x, y) => x - y)
	return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))]
}

test("calls canary: настоящие аудио и видео звонки параллельно k6", async ({ browser }) => {
	const contexts: BrowserContext[] = []
	const samples: Sample[] = []
	try {
		await wait(START_DELAY_SECONDS * 1000)
		await Promise.all(Array.from({ length: CANARY_PAIRS }, (_, i) => runPair(browser, i, samples, contexts)))
	} finally {
		for (const context of contexts) await context.close().catch(() => undefined)
	}

	const started = samples.length
	const active = samples.filter((s) => s.active).length
	const media = samples.filter((s) => s.mediaFlowing).length
	const failed = started - media
	const setups = samples.filter((s) => s.active).map((s) => s.setupMs)
	// Строка печатается ДО утверждений: нужна и на упавшем прогоне.
	console.log(
		`CANARY_CALLS_METRICS started=${started} active=${active} media=${media} failed=${failed} ` +
			`setup_p50_ms=${percentile(setups, 50)} setup_p95_ms=${percentile(setups, 95)} ` +
			`audio=${samples.filter((s) => s.kind === "audio").length} video=${samples.filter((s) => s.kind === "video").length}`,
	)
	expect(started, "хотя бы один звонок начат").toBeGreaterThan(0)
	expect(failed, "звонки без медиа").toBe(0)
})
