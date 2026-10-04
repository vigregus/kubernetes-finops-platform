import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import type { BrowserContext } from "@playwright/test"
import { STATE_FILE, fixtureFor, signIn } from "./support/auth"
import type { SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G4: «печатает» (`RT-001`).
 *
 * Два настоящих браузера против стенда. A набирает текст — B видит индикатор
 * в ленте и в шапке, а когда A перестаёт, индикатор гаснет. Главное — гаснет
 * **сам**: у A отнимается сеть, и `stop` отправить нечем (закрытый ноутбук,
 * убитая вкладка), но у B индикатор исчезает по таймауту получателя.
 *
 * Подделка автора и флуд проверяются на уровне протокола
 * (`tests/integration/typing_check.py`): браузер публикует только то, что
 * разрешает его собственный клиент, а честный клиент до лимита не дорастает.
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

test.describe("G4: «печатает»", () => {
	let a: SignedIn
	let b: SignedIn
	let conversationId: string
	const contexts: BrowserContext[] = []

	test.beforeAll(async ({ browser }) => {
		conversationId = state().conversationId
		a = await signIn(browser, fixtureFor("A"))
		b = await signIn(browser, fixtureFor("B"))
		contexts.push(a.context, b.context)
		await openConversation(a, conversationId)
		await openConversation(b, conversationId)
	})

	test.afterAll(async () => {
		for (const context of contexts) await context.close()
	})

	const indicator = () => b.page.locator("[data-typing-indicator]")

	test("RT-001: A печатает — B видит индикатор, A очистил поле — индикатор гаснет", async () => {
		await expect(indicator()).toHaveCount(0)

		await a.page.locator("[data-composer-input]").pressSequentially("привет", { delay: 60 })
		await expect(indicator()).toHaveCount(1, { timeout: 10_000 })
		await expect(indicator()).toContainText("is typing")
		await expect(b.page.getByText("is typing…")).toBeVisible()

		await a.page.locator("[data-composer-input]").fill("")
		await expect(indicator()).toHaveCount(0, { timeout: 10_000 })
	})

	test("RT-001: отправка сообщения гасит индикатор сразу", async () => {
		const text = `набор ${Date.now()}`
		await a.page.locator("[data-composer-input]").pressSequentially(text, { delay: 30 })
		await expect(indicator()).toHaveCount(1, { timeout: 10_000 })

		await a.page.locator("[data-composer-send]").click()
		await expect(b.page.locator("[data-message-id]", { hasText: text })).toHaveCount(1, {
			timeout: 30_000,
		})
		await expect(indicator()).toHaveCount(0, { timeout: 5_000 })
	})

	test("RT-001: без stop индикатор гаснет сам по таймауту получателя", async () => {
		await a.page.locator("[data-composer-input]").pressSequentially("не дописал", { delay: 40 })
		await expect(indicator()).toHaveCount(1, { timeout: 10_000 })

		// Связь у A пропала: `stop` отправить нечем.
		await a.context.setOffline(true)
		const lostAt = Date.now()
		await expect(indicator()).toHaveCount(0, { timeout: 8_000 })
		// Срок получателя — 5 с; гаснет не позже, чем через ~шесть, и не мгновенно.
		expect(Date.now() - lostAt).toBeGreaterThan(1500)

		await a.context.setOffline(false)
		await a.page.locator("[data-composer-input]").fill("")
		await expect(a.page.locator("[data-connection-state]")).toHaveAttribute(
			"data-connection-state",
			"connected",
			{ timeout: 60_000 },
		)
	})
})
