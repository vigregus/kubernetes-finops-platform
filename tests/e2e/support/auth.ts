import { expect } from "@playwright/test"
import type { Browser, BrowserContext, Page, Response } from "@playwright/test"
// Единый источник имени ключа — модуль клиента. Копия строки разошлась бы с ним
// молча: вход завёл бы второе устройство, и `devices` выросли бы на пустом месте.
import { DEVICE_ID_KEY } from "../../../apps/web/src/features/auth/deviceId"

/**
 * Общие помощники браузерной приёмки: вход, заголовки, состояние прогона.
 *
 * Здесь нет `test()` верхнего уровня, и это несущее свойство, а не стиль.
 * Модуль с `test()` верхнего уровня, импортированный спекой, был бы собран в
 * проект спеки — то есть подготовка побежала бы второй раз, уже внутри приёмки
 * (`fixture.setup.ts`).
 *
 * Разделение ролей отсюда же: подготовка заводит беседу и записывает состояние
 * файлом, спеки проверяют поведение. Но **входят** обе стороны одним и тем же
 * настоящим authorization-code: второй копии логики входа в репозитории нет,
 * потому что разошедшаяся копия означала бы, что приёмка проверяет не тот вход,
 * которым ходят люди.
 */

// Базовый путь — тот же, что у обёртки клиента (`src/api/client.ts`,
// `API_BASE_PATH`). Абсолютный адрес из `servers` контракта здесь был бы
// неверен: браузер обязан звать собственный origin.
export const API = "/api/v1"

/** Окружение задаёт оркестратор; без него приёмка не запускается вовсе. */
export function required(name: string): string {
	const value = process.env[name]
	if (!value) {
		throw new Error(
			`${name} не задан: приёмка запускается только через \`make web-e2e\` ` +
				`(scripts/web-e2e-fixture.sh), а не вызовом раннера напрямую`,
		)
	}
	return value
}

export const BASE_URL = required("E2E_BASE_URL")
export const IDP_ORIGIN = required("E2E_IDP_ORIGIN")
export const STATE_FILE = required("E2E_STATE_FILE")
export const APP_ORIGIN = new URL(BASE_URL).origin

export interface Fixture {
	email: string
	password: string
	deviceId: string
}

/** Сторона приёмки по роли. Пароли живут только в памяти процесса прогона. */
export function fixtureFor(role: "A" | "B"): Fixture {
	return {
		email: required(`E2E_USER_${role}_EMAIL`),
		password: required(`E2E_USER_${role}_PASSWORD`),
		deviceId: required(`E2E_USER_${role}_DEVICE_ID`),
	}
}

/**
 * Состояние прогона: его пишет проект `fixture`, читают спеки.
 *
 * Токенов и паролей здесь нет и быть не может — файл лежит на диске, а токен
 * живёт в памяти процесса прогона.
 */
export interface State {
	conversationId: string
	userIds: { a: string; b: string }
	startedAt: string
}

export interface SignedIn {
	context: BrowserContext
	page: Page
	token: string
}

export const isCallback = (response: Response): boolean =>
	response.request().method() === "POST" && response.url().includes("/auth/callback")

/**
 * Заголовки наших собственных вызовов.
 *
 * `X-Device-Id` здесь не украшение: без него сервер чеканит новое устройство на
 * каждый вызов (`_device_for` при отсутствии заголовка выдаёт uuid4), и `devices`
 * растут от одного лишь чтения. Заголовок идёт и на те вызовы, где тело есть, —
 * читается он из заголовка на всех защищённых путях, кроме `/auth/callback`, где
 * дублируется в теле.
 */
export function headers(fixture: Fixture, token: string): Record<string, string> {
	return { Authorization: `Bearer ${token}`, "X-Device-Id": fixture.deviceId }
}

/** Вход настоящим authorization-code: `directAccessGrantsEnabled: false`, парольным грантом токен не взять. */
export async function signIn(browser: Browser, fixture: Fixture): Promise<SignedIn> {
	const context = await browser.newContext({
		baseURL: BASE_URL,
		// Локальный удостоверяющий центр стенда; `browser.newContext()` не
		// наследует `use` проекта, поэтому задаётся здесь явно.
		ignoreHTTPSErrors: true,
	})

	try {
		// Идентификатор кладётся в хранилище до первого скрипта страницы — иначе
		// клиент заведёт свой, и фиксированный `device_id` не сработает.
		await context.addInitScript(
			({ key, value, origin }: { key: string; value: string; origin: string }) => {
				// Не наш origin (пустой документ, Keycloak) — не трогаем: хранилище
				// там либо недостижимо, либо чужое.
				if (window.location.origin !== origin) return
				window.localStorage.setItem(key, value)
			},
			{ key: DEVICE_ID_KEY, value: fixture.deviceId, origin: APP_ORIGIN },
		)

		const page = await context.newPage()
		await page.goto("/")

		const toKeycloak = page.waitForURL((url) => url.href.startsWith(IDP_ORIGIN))
		await page.getByRole("button", { name: "Continue with Vector ID" }).click()
		await toKeycloak

		await page.fill("#username", fixture.email)
		await page.fill("#password", fixture.password)

		// Токен берётся перехватом ответа приложения: наружу клиент его не
		// отдаёт и не должен. Одновременно это доказательство, что обмен
		// действительно произошёл, а не что страница просто открылась.
		const [callback] = await Promise.all([
			page.waitForResponse(isCallback),
			page.click("#kc-login"),
		])
		expect(callback.status(), `обмен кода на токен для ${fixture.email}`).toBe(200)

		const body = (await callback.json()) as { access_token?: unknown }
		expect(typeof body.access_token, "тело обмена несёт access_token").toBe("string")

		return { context, page, token: body.access_token as string }
	} catch (error) {
		await context.close()
		throw error
	}
}

/** `user_id` стороны: поиска пользователей в контракте нет, каждый берётся из своего `/me`. */
export async function whoAmI(signedIn: SignedIn, fixture: Fixture): Promise<string> {
	const response = await signedIn.page.request.get(`${API}/me`, {
		headers: headers(fixture, signedIn.token),
	})
	expect(response.status(), `GET /me для ${fixture.email}`).toBe(200)

	const body = (await response.json()) as { user_id?: unknown }
	expect(typeof body.user_id, "GET /me отдаёт user_id").toBe("string")
	return body.user_id as string
}
