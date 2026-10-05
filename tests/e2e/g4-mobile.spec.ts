import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import { STATE_FILE, fixtureFor, signIn } from "./support/auth"
import type { SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G4: мобильный интерфейс (`MOB-001…002`).
 *
 * Один настоящий браузер с экраном телефона (390×844, касания, `isMobile`) против
 * стенда: список бесед → беседа → отправка → «назад». Проверяется то, что видит
 * человек: экран «список» не поднимает беседу (нет композера и соединения), открытие
 * показывает шапку со стрелкой, системная кнопка «назад» возвращает к списку.
 *
 * Отдельно — широкий экран: левая колонка сворачивается в узкую (аватары) и разворачивается,
 * выбор переживает перезагрузку (`MOB-006`).
 *
 * Чего здесь нет: реальный телефон и его жесты (ручная проверка `MOB-004`).
 */

const state = (): State => JSON.parse(readFileSync(STATE_FILE, "utf-8")) as State

test.describe("G4: мобильный интерфейс", () => {
	let a: SignedIn

	test.beforeAll(async ({ browser }) => {
		a = await signIn(browser, fixtureFor("A"), {
			viewport: { width: 390, height: 844 },
			isMobile: true,
			hasTouch: true,
			deviceScaleFactor: 2,
		})
	})

	test.afterAll(async () => {
		await a.context.close()
	})

	test("MOB-001: список → беседа → «назад»; беседа не поднимается, пока её не открыли", async () => {
		const page = a.page
		const conversationId = state().conversationId
		await page.goto("/")

		// Экран «список»: карточки есть, панели беседы и композера нет.
		await expect(page.locator("[data-screen]")).toHaveAttribute("data-screen", "list")
		await expect(page.locator(`[data-conversation-id="${conversationId}"]`)).toBeVisible({ timeout: 60_000 })
		await expect(page.locator("[data-composer-input]")).toHaveCount(0)
		await expect(page.locator("[data-connection-state]")).toHaveCount(0)

		// Открыть беседу: список уходит, появляется шапка со стрелкой и композер.
		await page.locator(`[data-conversation-id="${conversationId}"]`).tap()
		await expect(page.locator("[data-screen]")).toHaveAttribute("data-screen", "chat")
		await expect(page.getByRole("button", { name: "Back to chats" })).toBeVisible()
		await expect(page.locator("[data-connection-state]")).toHaveAttribute("data-connection-state", "connected", {
			timeout: 60_000,
		})
		await expect(page.locator(`[data-conversation-id="${conversationId}"]`)).toHaveCount(0)

		// Отправить сообщение с телефона.
		const text = `mobile ${Date.now()}`
		await page.locator("[data-composer-input]").fill(text)
		await page.locator("[data-composer-send]").tap()
		await expect(page.getByText(text)).toBeVisible({ timeout: 30_000 })

		// «Назад» стрелкой — снова список.
		await page.getByRole("button", { name: "Back to chats" }).tap()
		await expect(page.locator("[data-screen]")).toHaveAttribute("data-screen", "list")
		await expect(page.locator("[data-composer-input]")).toHaveCount(0)

		// Открыть ещё раз и вернуться системной кнопкой «назад» (шаг истории).
		await page.locator(`[data-conversation-id="${conversationId}"]`).tap()
		await expect(page.locator("[data-screen]")).toHaveAttribute("data-screen", "chat")
		await page.goBack()
		await expect(page.locator("[data-screen]")).toHaveAttribute("data-screen", "list")
	})

	test("MOB-002: экран не прокручивается вбок, композер виден целиком", async () => {
		const page = a.page
		const conversationId = state().conversationId
		await page.goto("/")
		await page.locator(`[data-conversation-id="${conversationId}"]`).tap()
		await expect(page.locator("[data-composer-input]")).toBeVisible({ timeout: 60_000 })

		const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
		expect(overflow).toBeLessThanOrEqual(0)
		const box = await page.locator("[data-composer-send]").boundingBox()
		expect(box).not.toBeNull()
		expect(box!.x + box!.width).toBeLessThanOrEqual(390)
		expect(box!.y + box!.height).toBeLessThanOrEqual(844)
	})

	test("MOB-006: широкий экран — левая колонка сворачивается в аватары, выбор переживает перезагрузку", async ({ browser }) => {
		const desktop = await signIn(browser, fixtureFor("B"), { viewport: { width: 1280, height: 800 } })
		try {
			const page = desktop.page
			const conversationId = state().conversationId
			await page.goto("/")
			await expect(page.locator("[data-screen]")).toHaveAttribute("data-screen", "both")
			await expect(page.locator("[data-sidebar='collapsed']")).toHaveCount(0)

			await page.getByRole("button", { name: "Collapse chats panel" }).click()
			await expect(page.locator("[data-sidebar='collapsed']")).toBeVisible()
			// Беседа находима тем же адресом и открывается кликом по аватару.
			await page.locator(`[data-conversation-id="${conversationId}"]`).click()
			await expect(page.locator("[data-composer-input]")).toBeVisible({ timeout: 60_000 })

			await page.reload()
			await expect(page.locator("[data-sidebar='collapsed']")).toBeVisible({ timeout: 60_000 })

			await page.getByRole("button", { name: "Expand chats panel" }).click()
			await expect(page.locator("[data-sidebar='collapsed']")).toHaveCount(0)
		} finally {
			await desktop.context.close()
		}
	})
})
