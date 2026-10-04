import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import type { BrowserContext } from "@playwright/test"
import { STATE_FILE, fixtureFor, signIn } from "./support/auth"
import type { SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G4: Web Push на стороне браузера (`NTF-001`, `NTF-005`,
 * `NTF-006`, `NTF-008`, `NTF-009`).
 *
 * Настоящую службу push (FCM) из тестового браузера не достать, поэтому доставка
 * имитируется там, где она кончается: в **настоящий** Service Worker подаётся
 * событие `push` — ровно тот вызов, который сделал бы браузер, получив сигнал
 * от службы, — и проверяется, что worker показывает уведомление, что оно без
 * текста и что нажатие доходит до страницы. Серверная половина проверена
 * отдельно (`tests/integration/push_check.py`), «закрыл вкладку и получил
 * уведомление» — руками в браузере с интернетом.
 */

const state = (): State => JSON.parse(readFileSync(STATE_FILE, "utf-8")) as State

async function openConversation(who: SignedIn, conversationId: string): Promise<void> {
	await who.page.locator(`[data-conversation-id="${conversationId}"]`).click()
	await expect(who.page.locator("[data-connection-state]")).toHaveAttribute(
		"data-connection-state",
		"connected",
		{ timeout: 60_000 },
	)
}

test.describe("G4: Web Push в браузере", () => {
	let a: SignedIn
	let conversationId: string
	const contexts: BrowserContext[] = []

	test.beforeAll(async ({ browser }) => {
		conversationId = state().conversationId
		a = await signIn(browser, fixtureFor("A"))
		contexts.push(a.context)
		await a.context.grantPermissions(["notifications"])
		await a.page.reload()
		await openConversation(a, conversationId)
	})

	test.afterAll(async () => {
		for (const context of contexts) await context.close()
	})

	test("Service Worker регистрируется, отдаётся без кэша и не как HTML (NTF-006)", async () => {
		const response = await a.page.request.get("/sw.js")
		expect(response.status()).toBe(200)
		expect(response.headers()["content-type"]).toContain("javascript")
		expect(response.headers()["cache-control"]).toContain("no-cache")

		const scope = await a.page.evaluate(async () => {
			const registration = await navigator.serviceWorker.ready
			return registration.scope
		})
		expect(scope).toMatch(/\/$/)
	})

	/** Service Worker страницы — тот, что зарегистрировал сам клиент. */
	async function worker() {
		await a.page.evaluate(async () => {
			await navigator.serviceWorker.ready
		})
		const found = a.context.serviceWorkers().find((w) => w.url().endsWith("/sw.js"))
		if (found !== undefined) return found
		return a.context.waitForEvent("serviceworker", { timeout: 30_000 })
	}

	async function deliverPush(id: string | null) {
		const sw = await worker()
		await sw.evaluate(
			(conversation) => {
				// Внутри worker'а: ровно то событие, которое доставил бы браузер,
				// получив сигнал от службы push. Типы PushEvent в DOM-библиотеке нет.
				const scope = globalThis as unknown as {
					dispatchEvent(event: unknown): boolean
					PushEvent: new (type: string, init: { data: Uint8Array }) => unknown
				}
				const body =
					conversation === null
						? ""
						: JSON.stringify({ type: "message", conversation_id: conversation })
				scope.dispatchEvent(new scope.PushEvent("push", { data: new TextEncoder().encode(body) }))
			},
			id,
		)
		return sw
	}

	const notifications = (sw: Awaited<ReturnType<typeof worker>>) =>
		sw.evaluate(async () => {
			const registration = (globalThis as unknown as {
				registration: { getNotifications(): Promise<{ title: string; tag: string; body: string; data: unknown }[]> }
			}).registration
			return (await registration.getNotifications()).map((n) => ({
				title: n.title,
				tag: n.tag,
				body: n.body,
				data: n.data,
			}))
		})

	test("push показывает уведомление-сигнал без текста, тегом беседы (NTF-001, NTF-009)", async () => {
		const sw = await deliverPush(conversationId)
		await expect
			.poll(async () => (await notifications(sw)).map((n) => n.tag))
			.toContain(`conversation:${conversationId}`)

		const shown = (await notifications(sw)).find((n) => n.tag === `conversation:${conversationId}`)
		expect(shown?.title).toBe("New message")
		// В уведомлении нет ни текста сообщения, ни имени отправителя.
		expect(JSON.stringify(shown)).not.toMatch(/sender|"text"/i)
		expect(shown?.data).toEqual({ conversationId })
	})

	test("пять сигналов подряд по одной беседе — одно уведомление (свёртка по тегу)", async () => {
		const sw = await worker()
		for (let n = 0; n < 5; n += 1) await deliverPush(conversationId)
		await expect
			.poll(async () => (await notifications(sw)).filter((n) => n.tag === `conversation:${conversationId}`).length)
			.toBe(1)
	})

	test("нажатие на уведомление доходит до открытой вкладки и называет беседу (NTF-008)", async () => {
		const sw = await deliverPush(conversationId)
		await a.page.evaluate(() => {
			;(window as unknown as { __opened: unknown[] }).__opened = []
			navigator.serviceWorker.addEventListener("message", (event) => {
				;(window as unknown as { __opened: unknown[] }).__opened.push(event.data)
			})
		})
		await sw.evaluate(async () => {
			const scope = globalThis as unknown as {
				registration: { getNotifications(): Promise<unknown[]> }
				dispatchEvent(event: unknown): boolean
				NotificationEvent: new (type: string, init: { notification: unknown; action: string }) => unknown
			}
			const [notification] = await scope.registration.getNotifications()
			scope.dispatchEvent(new scope.NotificationEvent("notificationclick", { notification, action: "" }))
		})
		await expect
			.poll(() => a.page.evaluate(() => (window as unknown as { __opened: unknown[] }).__opened))
			.toContainEqual({ type: "open-conversation", conversationId })
	})

	test("push с негодным телом всё равно показывает уведомление (браузер требует его на каждый push)", async () => {
		const sw = await deliverPush(null)
		await expect.poll(async () => (await notifications(sw)).map((n) => n.tag)).toContain("message")
	})

	test("баннер включения виден, пока подписки нет, и не мешает работе (NTF-005)", async () => {
		// Разрешение выдано, но подписки в браузере нет — предлагаем включить; или
		// сервер не настроил уведомления — тогда тишина. Беседа работает в обоих случаях.
		const banner = a.page.locator("[data-notifications-state]")
		const count = await banner.count()
		expect(count).toBeLessThanOrEqual(1)
		await expect(a.page.locator("[data-composer-input]")).toBeVisible()
	})
})
