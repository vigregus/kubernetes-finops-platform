import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import type { Frame, Response } from "@playwright/test"

/**
 * Браузерная приёмка G3-005: `web` поднят, вход и список бесед работают.
 *
 * Проверка идёт **против стенда и с хоста**: только публичные имена, ни одного
 * обращения внутрь кластера и ни одного туннеля к нему. Иначе «работает» означало
 * бы «работает в обход того пути, которым пойдёт браузер», — а весь смысл здесь
 * в том, что край, Keycloak, cookie и origin сходятся **снаружи**.
 *
 * Ни одного оператора удаления в спеке нет и быть не может: уборка живёт в
 * `scripts/web-e2e-fixture.sh` и охватывает сам Playwright — контексты, сессии
 * стенда, затем две таблицы одной транзакцией по закрытому списку двух `user_id`.
 * Спеке для этого не нужно ничего: она не владеет ничем, что пережило бы прогон.
 *
 * Адреса приходят из окружения: их задаёт оркестратор. Спека не запускается
 * вызовом раннера напрямую, и падает в этом случае внятно, а не «Invalid URL».
 */

const API = "/api/v1"

function required(name: string): string {
	const value = process.env[name]
	if (!value) {
		throw new Error(
			`${name} не задан: приёмка запускается только через \`make web-e2e\` ` +
				`(scripts/web-e2e-fixture.sh), а не вызовом раннера напрямую`,
		)
	}
	return value
}

const BASE_URL = required("E2E_BASE_URL")
const IDP_ORIGIN = required("E2E_IDP_ORIGIN")
const STATE_FILE = required("E2E_STATE_FILE")

const APP_ORIGIN = new URL(BASE_URL).origin
const USER_A = {
	email: required("E2E_USER_A_EMAIL"),
	password: required("E2E_USER_A_PASSWORD"),
}

/** Состояние прогона, записанное проектом `fixture`. */
interface State {
	conversationId: string
	userIds: { a: string; b: string }
	startedAt: string
}

const method = (expected: string, path: string) => (response: Response): boolean =>
	response.request().method() === expected && new URL(response.url()).pathname === path

const isCallback = method("POST", `${API}/auth/callback`)
const isMe = method("GET", `${API}/me`)
const isConversations = method("GET", `${API}/conversations`)
const isRefresh = method("POST", `${API}/auth/refresh`)

test("вход, список бесед и восстановление состояния после перезагрузки", async ({ page }) => {
	const state = JSON.parse(readFileSync(STATE_FILE, "utf8")) as State

	await test.step("у первого посетителя просят вход, и это не «сессия истекла»", async () => {
		await page.goto("/")

		await expect(page.getByRole("button", { name: "Continue with Vector ID" })).toBeVisible()
		// Cookie у нового посетителя нет вовсе, поэтому «истекла» было бы
		// утверждением о сессии, которой не было.
		await expect(
			page.getByText(/session expired/i),
			"первому посетителю не сообщают об истёкшей сессии",
		).toHaveCount(0)
	})

	await test.step("кнопка уводит на настоящий Keycloak, а не на заглушку", async () => {
		const toKeycloak = page.waitForURL((url) => url.href.startsWith(IDP_ORIGIN))
		await page.getByRole("button", { name: "Continue with Vector ID" }).click()
		await toKeycloak

		const authorize = new URL(page.url())
		expect(authorize.origin, "вход идёт через настоящий источник идентичности").toBe(IDP_ORIGIN)
		expect(authorize.pathname).toContain("/protocol/openid-connect/auth")
		expect(authorize.searchParams.get("client_id")).toBe("messenger-web")
		expect(authorize.searchParams.get("redirect_uri")).toBe(`${APP_ORIGIN}/callback`)
	})

	await test.step("обмен кода и данные bootstrap приходят от API", async () => {
		// Все ожидания ставятся **до** отправки формы: ответы приложения начнут
		// приходить сразу после обмена, и зарегистрированное позже ожидание
		// промахнулось бы мимо уже случившегося события.
		const callback = page.waitForResponse(isCallback)
		const me = page.waitForResponse(isMe)
		const conversations = page.waitForResponse(isConversations)

		await page.fill("#username", USER_A.email)
		await page.fill("#password", USER_A.password)
		await page.click("#kc-login")

		expect((await callback).status(), "обмен кода на токен").toBe(200)

		const account = await me
		expect(account.status(), "GET /me").toBe(200)
		const accountBody = (await account.json()) as { user_id?: unknown }
		expect(accountBody.user_id, "в приложении — та самая учётная запись фикстуры").toBe(
			state.userIds.a,
		)

		const listed = await conversations
		expect(listed.status(), "GET /conversations").toBe(200)
		const page1 = (await listed.json()) as { items?: Array<{ conversation_id?: unknown }> }
		// Ответ API, а не только разметка: беседа заведена подготовкой этого же
		// прогона, и её идентификатор известен только ему.
		expect(
			(page1.items ?? []).map((item) => item.conversation_id),
			"API отдал беседу, заведённую подготовкой",
		).toContain(state.conversationId)
	})

	const row = page.locator(`[data-conversation-id="${state.conversationId}"]`)

	await test.step("беседа находится по идентификатору, а не по имени", async () => {
		// Локатор по атрибуту, а не по тексту: имя — строка, которую фикстура
		// могла бы совпасть со стендовыми данными случайно, атрибут — нет.
		await expect(row).toBeVisible()
	})

	await test.step("перезагрузка восстанавливает состояние, не уводя в Keycloak", async () => {
		let leftToKeycloak = false
		const watch = (frame: Frame): void => {
			if (frame === page.mainFrame() && frame.url().startsWith(IDP_ORIGIN)) leftToKeycloak = true
		}
		page.on("framenavigated", watch)

		// Access token живёт только в памяти вкладки; после перезагрузки его нет,
		// и состояние восстанавливается refresh-cookie — то есть тем самым
		// путём, ради которого cookie заведена.
		const refresh = page.waitForResponse(isRefresh)
		await page.reload()

		expect((await refresh).status(), "refresh по cookie").toBe(200)
		expect(leftToKeycloak, "восстановление идёт по cookie, а не новым входом").toBe(false)
		expect(new URL(page.url()).origin, "страница осталась на своём origin").toBe(APP_ORIGIN)

		await expect(row).toBeVisible()
	})
})
