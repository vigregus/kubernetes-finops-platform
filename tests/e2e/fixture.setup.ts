import { mkdirSync, writeFileSync } from "node:fs"
import { dirname } from "node:path"
import { expect, test } from "@playwright/test"
import type { BrowserContext } from "@playwright/test"
import { API, STATE_FILE, fixtureFor, headers, signIn, whoAmI } from "./support/auth"
import type { Fixture, State } from "./support/auth"

/**
 * Подготовка фикстур приёмки.
 *
 * Проект `fixture` в `playwright.config.ts`; приёмки (`acceptance`, `g3-006`)
 * объявляют его зависимостью, поэтому порядок задан раннером. Состояние
 * передаётся **файлом**, а не импортом модуля: модуль с `test()` верхнего
 * уровня, импортированный спекой, был бы собран в проект спеки — то есть
 * подготовка побежала бы второй раз, уже внутри приёмки.
 *
 * Вход и заголовки живут в `support/auth.ts` и ровно в одном экземпляре: второй
 * копии логики входа в репозитории быть не должно, иначе `fixture` и `g3-006`
 * однажды разойдутся, и приёмка будет проверять не тот вход, которым ходят люди.
 *
 * Уборка сюда не входит: `DELETE` живёт только в `scripts/web-e2e-fixture.sh`,
 * потому что контракт требует порядка `contexts → logout в Keycloak →
 * realtime_connections → sessions` одной транзакцией по закрытому списку двух
 * `user_id`, и этот порядок охватывает сам Playwright. Здесь же контексты
 * только закрываются — это шаг 1 того же порядка, выполненный тем, кто их открыл.
 */

const A: Fixture = fixtureFor("A")
const B: Fixture = fixtureFor("B")

test("две стороны входят настоящим входом, беседа заводится идемпотентно", async ({ browser }) => {
	const contexts: BrowserContext[] = []

	// Контекст попадает в список сразу после открытия — иначе падение второго
	// входа оставило бы первый открытым, а следующий прогон подхватил бы его
	// живую сессию, и уборка снова оказалась бы полумерой.
	const open = async (fixture: Fixture) => {
		const signedIn = await signIn(browser, fixture)
		contexts.push(signedIn.context)
		return signedIn
	}

	try {
		const a = await open(A)
		const b = await open(B)

		const userIdA = await whoAmI(a, A)
		const userIdB = await whoAmI(b, B)

		// Беседа заводится от лица A: ту же сторону приёмки потом и смотрят.
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
