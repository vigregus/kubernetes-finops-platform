import { readFileSync } from "node:fs"
import { expect, test } from "@playwright/test"
import type { BrowserContext } from "@playwright/test"
import { STATE_FILE, fixtureFor, signIn } from "./support/auth"
import type { SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G4: вложения (`ATT-002`, `ATT-003`, `ATT-005`, `ATT-007`).
 *
 * Два настоящих браузера против стенда: человек A выбирает файл в композере,
 * браузер сам идёт в MinIO по предподписанной ссылке (`PUT` без токена), а B
 * видит картинку или файл без перезагрузки и скачивает байты по ссылке. Отказы
 * проверяются по тем же словам, которые увидит человек.
 *
 * Адреса и пароли задаёт оркестратор (`scripts/web-e2e-fixture.sh`), как у
 * соседних спек; спека читает разметку композера (`data-composer-file`,
 * `data-attachment-draft`) и ленты (`data-message-id`).
 */

const state = (): State => JSON.parse(readFileSync(STATE_FILE, "utf-8")) as State

// Минимальный настоящий PNG 1x1: сигнатура и IHDR/IDAT/IEND — то, что принимает
// сверка типа на сервере (`ATT-007`), а не просто «байты с расширением».
const PNG = Buffer.from(
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==",
	"base64",
)
const EXE_AS_PNG = Buffer.concat([Buffer.from("MZ\x90\x00"), Buffer.alloc(64)])
// Тестовая строка антивирусов, собранная из частей (исходник сам не должен
// срабатывать на антивирусе разработчика).
const EICAR = Buffer.from(
	"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR" + "-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*",
)

async function openConversation(who: SignedIn, conversationId: string): Promise<void> {
	await who.page.locator(`[data-conversation-id="${conversationId}"]`).click()
	await expect(who.page.locator("[data-connection-state]")).toHaveAttribute(
		"data-connection-state",
		"connected",
		{ timeout: 60_000 },
	)
}

async function pick(who: SignedIn, file: { name: string; mimeType: string; buffer: Buffer }) {
	await who.page.locator("[data-composer-file]").setInputFiles(file)
}

const draft = (who: SignedIn) => who.page.locator("[data-attachment-draft]")

test.describe("G4: вложения", () => {
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

	test("ATT-003: картинка от A доходит до B без перезагрузки и скачивается", async () => {
		await pick(a, { name: "cat.png", mimeType: "image/png", buffer: PNG })
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "ready", { timeout: 60_000 })

		const caption = `смотри ${Date.now()}`
		await a.page.locator("[data-composer-input]").fill(caption)
		await a.page.locator("[data-composer-send]").click()

		const row = b.page.locator("[data-message-id]", { hasText: caption })
		await expect(row).toHaveCount(1, { timeout: 60_000 })
		const image = row.locator("img[alt='cat.png']")
		await expect(image).toBeVisible({ timeout: 30_000 })

		const src = await image.getAttribute("src")
		expect(src, "у картинки есть ссылка на скачивание").toBeTruthy()
		const downloaded = await b.page.request.get(src as string)
		expect(downloaded.status()).toBe(200)
		expect(Buffer.compare(await downloaded.body(), PNG), "скачанное равно загруженному").toBe(0)

		// Черновик у отправителя очищен, а сообщение пережило перезагрузку:
		// история несёт вложение так же, как живая доставка.
		await expect(draft(a)).toHaveCount(0)
		await b.page.reload()
		await openConversation(b, conversationId)
		await expect(
			b.page.locator("[data-message-id]", { hasText: caption }).locator("img[alt='cat.png']"),
		).toBeVisible({ timeout: 30_000 })
	})

	test("ATT-003: файл (PDF) доходит с именем", async () => {
		const pdf = Buffer.from("%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")
		await pick(a, { name: "договор.pdf", mimeType: "application/pdf", buffer: pdf })
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "ready", { timeout: 60_000 })
		await a.page.locator("[data-composer-send]").click()

		await expect(b.page.getByText("договор.pdf").first()).toBeVisible({ timeout: 60_000 })
	})

	test("ATT-005: неразрешённый тип отвергается сразу, до загрузки", async () => {
		await pick(a, { name: "setup.exe", mimeType: "application/x-msdownload", buffer: EXE_AS_PNG })
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "failed", { timeout: 15_000 })
		await expect(draft(a)).toContainText("isn't supported")
		await a.page.locator("[data-attachment-clear]").click()
	})

	test("ATT-007: исполняемый файл под видом картинки отклонён при обработке", async () => {
		await pick(a, { name: "cat.png", mimeType: "image/png", buffer: EXE_AS_PNG })
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "failed", { timeout: 60_000 })
		await expect(draft(a)).toContainText("doesn't match")
		await a.page.locator("[data-attachment-clear]").click()
	})

	test("ATT-002: заражённый файл (EICAR) заблокирован, сообщение не создаётся", async () => {
		await pick(a, { name: "note.txt", mimeType: "text/plain", buffer: EICAR })
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "failed", { timeout: 60_000 })
		await expect(draft(a)).toContainText("unsafe")
		await a.page.locator("[data-attachment-clear]").click()
	})
})
