import { mkdirSync, writeFileSync } from "node:fs"
import { dirname } from "node:path"
import { expect, test } from "@playwright/test"
import type { Browser, BrowserContext, Page, Response } from "@playwright/test"
// Единый источник имени ключа — модуль клиента. Копия строки разошлась бы с ним
// молча: вход завёл бы второе устройство, и `devices` выросли бы на пустом месте.
import { DEVICE_ID_KEY } from "../../apps/web/src/features/auth/deviceId"

/**
 * Подготовка фикстур приёмки G3-005.
 *
 * Проект `fixture` в `playwright.config.ts`; приёмка (`acceptance`) объявляет
 * его зависимостью, поэтому порядок задан раннером. Состояние передаётся
 * **файлом**, а не импортом модуля: модуль с `test()` верхнего уровня,
 * импортированный спекой, был бы собран в проект спеки — то есть подготовка
 * побежала бы второй раз, уже внутри приёмки.
 *
 * Уборка сюда не входит: `DELETE` живёт только в `scripts/web-e2e-fixture.sh`,
 * потому что контракт требует порядка `contexts → logout в Keycloak →
 * realtime_connections → sessions` одной транзакцией по закрытому списку двух
 * `user_id`, и этот порядок охватывает сам Playwright. Здесь же контексты
 * только закрываются — это шаг 1 того же порядка, выполненный тем, кто их открыл.
 */

// Базовый путь — тот же, что у обёртки клиента (`src/api/client.ts`,
// `API_BASE_PATH`). Абсолютный адрес из `servers` контракта здесь был бы
// неверен: браузер обязан звать собственный origin.
const API = "/api/v1"

/** Окружение задаёт оркестратор; без него приёмка не запускается вовсе. */
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

interface Fixture {
	email: string
	password: string
	deviceId: string
}

const A: Fixture = {
	email: required("E2E_USER_A_EMAIL"),
	password: required("E2E_USER_A_PASSWORD"),
	deviceId: required("E2E_USER_A_DEVICE_ID"),
}

const B: Fixture = {
	email: required("E2E_USER_B_EMAIL"),
	password: required("E2E_USER_B_PASSWORD"),
	deviceId: required("E2E_USER_B_DEVICE_ID"),
}

/** Состояние прогона: его читает спека приёмки. */
interface State {
	conversationId: string
	userIds: { a: string; b: string }
	startedAt: string
}

interface SignedIn {
	context: BrowserContext
	page: Page
	token: string
}

const isCallback = (response: Response): boolean =>
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
function headers(fixture: Fixture, token: string): Record<string, string> {
	return { Authorization: `Bearer ${token}`, "X-Device-Id": fixture.deviceId }
}

/** Вход настоящим authorization-code: `directAccessGrantsEnabled: false`, парольным грантом токен не взять. */
async function signIn(browser: Browser, fixture: Fixture): Promise<SignedIn> {
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
async function whoAmI(signedIn: SignedIn, fixture: Fixture): Promise<string> {
	const response = await signedIn.page.request.get(`${API}/me`, {
		headers: headers(fixture, signedIn.token),
	})
	expect(response.status(), `GET /me для ${fixture.email}`).toBe(200)

	const body = (await response.json()) as { user_id?: unknown }
	expect(typeof body.user_id, "GET /me отдаёт user_id").toBe("string")
	return body.user_id as string
}

test("две стороны входят настоящим входом, беседа заводится идемпотентно", async ({ browser }) => {
	const contexts: BrowserContext[] = []

	// Контекст попадает в список сразу после открытия — иначе падение второго
	// входа оставило бы первый открытым, а следующий прогон подхватил бы его
	// живую сессию, и уборка снова оказалась бы полумерой.
	const open = async (fixture: Fixture): Promise<SignedIn> => {
		const signedIn = await signIn(browser, fixture)
		contexts.push(signedIn.context)
		return signedIn
	}

	try {
		const a = await open(A)
		const b = await open(B)

		const userIdA = await whoAmI(a, A)
		const userIdB = await whoAmI(b, B)

		// Беседа заводится от лица A: ту же сторону приёмка потом и смотрит.
		const created = await a.page.request.post(`${API}/conversations`, {
			headers: headers(A, a.token),
			data: { participant_id: userIdB },
		})
		expect(
			[200, 201],
			"createDirectConversation идемпотентна по составу участников: 201 в первый раз, 200 при повторе",
		).toContain(created.status())

		const conversation = (await created.json()) as { conversation_id?: unknown }
		expect(typeof conversation.conversation_id, "ответ несёт conversation_id").toBe("string")

		const state: State = {
			conversationId: conversation.conversation_id as string,
			userIds: { a: userIdA, b: userIdB },
			startedAt: new Date().toISOString(),
		}

		mkdirSync(dirname(STATE_FILE), { recursive: true })
		writeFileSync(STATE_FILE, `${JSON.stringify(state, null, 2)}\n`)

		// Печатается идентификатор беседы, а не токены и не пароли: отчёт
		// приёмки называет то, чем проверял, а не то, чем входил.
		console.log(
			`  фикстуры: ${A.email} и ${B.email}, беседа ${state.conversationId} (${created.status()})`,
		)
	} finally {
		for (const context of contexts) await context.close()
	}
})
