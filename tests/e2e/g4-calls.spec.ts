import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import type { BrowserContext, Page } from "@playwright/test"
import { STATE_FILE, fixtureFor, signIn } from "./support/auth"
import type { SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G4: звонки один-на-один (`CALL-001…014`, ADR 0007).
 *
 * Два настоящих браузера против стенда, с фейковыми камерой и микрофоном
 * Chromium (`playwright.config.ts`, проект `g4`). Проверяется то, что
 * доступно браузеру: сигнализация через API и Centrifugo, соединение WebRTC,
 * **медиа идёт** (счётчик принятых пакетов растёт — не «состояние connected»),
 * итог в ленте, несколько вкладок вызываемого.
 *
 * Чего здесь нет, и это названо: **обрыв сети посреди звонка** (10 с — выживает,
 * 20 с — завершается). `setOffline` у Playwright рвёт HTTP и WebSocket, но не
 * UDP-медиа WebRTC, то есть проверка показала бы не то, что заявлено. Правила
 * обрыва проверены прогоном движка без браузера (`callEngine.test.ts`).
 * Релей TURN и реальные сети — ручная проверка `CALL-011`.
 */

const state = (): State => JSON.parse(readFileSync(STATE_FILE, "utf-8")) as State

const PEER = (who: SignedIn) => who.page.locator("[data-call-phase]")

async function openConversation(who: SignedIn, conversationId: string): Promise<void> {
	await who.page.locator(`[data-conversation-id="${conversationId}"]`).click()
	await expect(who.page.locator("[data-connection-state]")).toHaveAttribute(
		"data-connection-state",
		"connected",
		{ timeout: 60_000 },
	)
}

/**
 * Записывает все соединения страницы, чтобы потом спросить у них статистику.
 * Продакшен-код не отдаёт соединение наружу (и не должен), поэтому приёмка
 * оборачивает конструктор до загрузки страницы.
 */
async function trackPeers(context: BrowserContext): Promise<void> {
	await context.addInitScript(() => {
		const Original = window.RTCPeerConnection
		const peers: RTCPeerConnection[] = []
		;(window as unknown as { __peers: RTCPeerConnection[] }).__peers = peers
		window.RTCPeerConnection = new Proxy(Original, {
			construct(target, args) {
				const peer = new target(...(args as [RTCConfiguration?]))
				peers.push(peer)
				return peer
			},
		})
	})
}

/** Сколько пакетов принято по входящим потокам; растёт — значит, медиа идёт. */
async function packetsReceived(page: Page): Promise<number> {
	return page.evaluate(async () => {
		const peers = (window as unknown as { __peers?: RTCPeerConnection[] }).__peers ?? []
		let total = 0
		for (const peer of peers) {
			const report = await peer.getStats()
			report.forEach((entry) => {
				if (entry.type === "inbound-rtp") total += entry.packetsReceived ?? 0
			})
		}
		return total
	})
}

async function expectMediaFlowing(page: Page): Promise<void> {
	const first = await packetsReceived(page)
	await expect.poll(() => packetsReceived(page), { timeout: 20_000 }).toBeGreaterThan(first)
	expect(first).toBeGreaterThan(0)
}

test.describe("G4: звонки", () => {
	let a: SignedIn
	let b: SignedIn
	let conversationId: string
	const contexts: BrowserContext[] = []

	test.beforeAll(async ({ browser }) => {
		conversationId = state().conversationId
		a = await signIn(browser, fixtureFor("A"))
		b = await signIn(browser, fixtureFor("B"))
		contexts.push(a.context, b.context)
		await trackPeers(a.context)
		await trackPeers(b.context)
		// Страницы перезагружаются: обёртка конструктора ставится до загрузки.
		await a.page.reload()
		await b.page.reload()
		await openConversation(a, conversationId)
		await openConversation(b, conversationId)
	})

	test.afterAll(async () => {
		for (const context of contexts) await context.close()
	})

	/** Звонок сводится в чистое состояние: что бы ни осталось от прошлого шага. */
	test.afterEach(async () => {
		for (const who of [a, b]) {
			const end = who.page.getByRole("button", { name: "End call" })
			if (await end.isVisible().catch(() => false)) await end.click()
			const decline = who.page.getByRole("button", { name: "Decline call" })
			if (await decline.isVisible().catch(() => false)) await decline.click()
			await expect(PEER(who)).toHaveCount(0, { timeout: 15_000 })
		}
	})

	test("CALL-001: A звонит B, B принимает — медиа идёт, оба кладут трубку, итог в ленте", async () => {
		await a.page.getByRole("button", { name: "Start voice call" }).click()
		await expect(PEER(a)).toHaveAttribute("data-call-phase", /outgoing|connecting|active/)

		const incoming = b.page.getByRole("alertdialog", { name: "Incoming call" })
		await expect(incoming).toBeVisible({ timeout: 20_000 })
		await expect(incoming).toHaveAttribute("data-call-kind", "audio")
		await b.page.getByRole("button", { name: "Accept call" }).click()

		await expect(PEER(a)).toHaveAttribute("data-call-phase", "active", { timeout: 40_000 })
		await expect(PEER(b)).toHaveAttribute("data-call-phase", "active", { timeout: 40_000 })
		await expectMediaFlowing(a.page)
		await expectMediaFlowing(b.page)

		// Звонок длится — таймер идёт.
		await expect(a.page.locator("[data-call-status]")).toHaveText(/^0:0[1-9]|^0:[1-5]\d/, { timeout: 15_000 })

		await b.page.getByRole("button", { name: "End call" }).click()
		await expect(PEER(a)).toHaveAttribute("data-call-reason", "completed", { timeout: 20_000 })
		await expect(PEER(b)).toHaveCount(1)

		// Итог — системное сообщение, у обоих, и в живой ленте, и после перезагрузки.
		for (const who of [a, b]) {
			await expect(who.page.locator('[data-call-summary="completed"]').last()).toBeVisible({
				timeout: 20_000,
			})
		}
		await a.page.reload()
		await openConversation(a, conversationId)
		const summary = a.page.locator('[data-call-summary="completed"]').last()
		await expect(summary).toBeVisible({ timeout: 20_000 })
		await expect(summary).toContainText(/Voice call, \d+:\d\d/)
	})

	test("CALL-010: видеозвонок — удалённое видео показывает кадры", async () => {
		await a.page.getByRole("button", { name: "Start video call" }).click()
		await expect(b.page.getByRole("alertdialog", { name: "Incoming call" })).toHaveAttribute(
			"data-call-kind",
			"video",
			{ timeout: 20_000 },
		)
		await b.page.getByRole("button", { name: "Accept call" }).click()
		await expect(PEER(a)).toHaveAttribute("data-call-phase", "active", { timeout: 40_000 })
		await expect(PEER(b)).toHaveAttribute("data-call-phase", "active", { timeout: 40_000 })

		for (const who of [a, b]) {
			await expect
				.poll(
					() =>
						who.page.evaluate(() => {
							const video = document.querySelector("video[data-call-remote]") as HTMLVideoElement | null
							return video === null ? 0 : video.videoWidth
						}),
					{ timeout: 30_000 },
				)
				.toBeGreaterThan(0)
		}
		await expectMediaFlowing(b.page)

		// Микрофон выключается, не разрывая соединения.
		await a.page.getByRole("button", { name: "Mute microphone" }).click()
		await expect(a.page.getByRole("button", { name: "Unmute microphone" })).toHaveAttribute(
			"aria-pressed",
			"true",
		)
		await expect(PEER(a)).toHaveAttribute("data-call-phase", "active")
	})

	test("CALL-002: B отклоняет — у A «declined», итог в ленте", async () => {
		await a.page.getByRole("button", { name: "Start voice call" }).click()
		await expect(b.page.getByRole("alertdialog", { name: "Incoming call" })).toBeVisible({ timeout: 20_000 })
		await b.page.getByRole("button", { name: "Decline call" }).click()

		await expect(PEER(a)).toHaveAttribute("data-call-reason", "declined", { timeout: 20_000 })
		await expect(a.page.locator('[data-call-summary="declined"]').last()).toBeVisible({ timeout: 20_000 })
		await expect(b.page.locator('[data-call-summary="declined"]').last()).toBeVisible({ timeout: 20_000 })
	})

	test("CALL-003: A отменяет, пока звонит — у B вызов гаснет, итог «cancelled»", async () => {
		await a.page.getByRole("button", { name: "Start voice call" }).click()
		await expect(b.page.getByRole("alertdialog", { name: "Incoming call" })).toBeVisible({ timeout: 20_000 })
		await a.page.getByRole("button", { name: "End call" }).click()
		await expect(b.page.getByRole("alertdialog", { name: "Incoming call" })).toHaveCount(0, { timeout: 20_000 })
		await expect(b.page.locator('[data-call-summary="cancelled"]').last()).toBeVisible({ timeout: 20_000 })
	})

	test("CALL-012: B открыл вторую вкладку, принял в одной — в другой вызов погас", async () => {
		const second = await b.context.newPage()
		await second.goto(b.page.url())
		await second.locator(`[data-conversation-id="${conversationId}"]`).click()
		await expect(second.locator("[data-connection-state]")).toHaveAttribute(
			"data-connection-state",
			"connected",
			{ timeout: 60_000 },
		)

		await a.page.getByRole("button", { name: "Start voice call" }).click()
		const first = b.page.getByRole("alertdialog", { name: "Incoming call" })
		const other = second.getByRole("alertdialog", { name: "Incoming call" })
		await expect(first).toBeVisible({ timeout: 20_000 })
		await expect(other).toBeVisible({ timeout: 20_000 })

		await first.getByRole("button", { name: "Accept call" }).click()
		await expect(other).toHaveCount(0, { timeout: 20_000 })
		await expect(PEER(a)).toHaveAttribute("data-call-phase", "active", { timeout: 40_000 })
		await second.close()
	})

	test("CALL-002: B не отвечает — через 30 с у A «No answer», в ленте «пропущенный»", async () => {
		test.setTimeout(120_000)
		await a.page.getByRole("button", { name: "Start voice call" }).click()
		await expect(b.page.getByRole("alertdialog", { name: "Incoming call" })).toBeVisible({ timeout: 20_000 })

		await expect(PEER(a)).toHaveAttribute("data-call-reason", "missed", { timeout: 60_000 })
		await expect(b.page.getByRole("alertdialog", { name: "Incoming call" })).toHaveCount(0, { timeout: 20_000 })
		await expect(b.page.locator('[data-call-summary="missed"]').last()).toContainText(/Missed voice call/, {
			timeout: 20_000,
		})
		await expect(a.page.locator('[data-call-summary="missed"]').last()).toContainText(/No answer/)
	})

	test("CALL-006: сигнал, отправленный не участником звонка, отвергается сервером", async ({ request }) => {
		// Прямой запрос без входа: тот же отказ, что и у вошедшего, но не участника.
		const response = await request.post(`/api/v1/calls/${"00000000-0000-4000-8000-000000000000"}/signals`, {
			data: { type: "offer", sdp: "v=0" },
		})
		expect(response.status()).toBe(401)
	})
})
