import { readFileSync } from "node:fs"
import { deflateSync } from "node:zlib"
import { expect, test } from "@playwright/test"
import type { BrowserContext } from "@playwright/test"
import { STATE_FILE, fixtureFor, signIn } from "./support/auth"
import type { SignedIn, State } from "./support/auth"

/**
 * Браузерная приёмка G4: вложения (`ATT-002`, `ATT-003`, `ATT-004`, `ATT-005`, `ATT-007`).
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

// Настоящий PNG заданного размера, собранный без зависимостей: для проверки
// миниатюры нужна картинка больше предела (480), а не точка 1x1.
function crc32(buffer: Buffer): number {
	let crc = 0xffffffff
	for (const byte of buffer) {
		crc ^= byte
		for (let bit = 0; bit < 8; bit++) crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1
	}
	return (crc ^ 0xffffffff) >>> 0
}
function chunk(type: string, data: Buffer): Buffer {
	const body = Buffer.concat([Buffer.from(type, "ascii"), data])
	const length = Buffer.alloc(4)
	length.writeUInt32BE(data.length)
	const crc = Buffer.alloc(4)
	crc.writeUInt32BE(crc32(body))
	return Buffer.concat([length, body, crc])
}
function bigPng(width: number, height: number): Buffer {
	const header = Buffer.alloc(13)
	header.writeUInt32BE(width, 0)
	header.writeUInt32BE(height, 4)
	header.set([8, 2, 0, 0, 0], 8) // 8 бит, RGB
	const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, 0x7f)])
	const raw = Buffer.concat(Array.from({ length: height }, () => row))
	return Buffer.concat([
		Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
		chunk("IHDR", header),
		chunk("IDAT", deflateSync(raw)),
		chunk("IEND", Buffer.alloc(0)),
	])
}
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

	test("ATT-003: большая картинка показывается миниатюрой, оригинал открывается по клику", async () => {
		const original = bigPng(1200, 800)
		await pick(a, { name: "big.png", mimeType: "image/png", buffer: original })
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "ready", { timeout: 60_000 })
		const caption = `большая ${Date.now()}`
		await a.page.locator("[data-composer-input]").fill(caption)
		await a.page.locator("[data-composer-send]").click()

		const row = b.page.locator("[data-message-id]", { hasText: caption })
		await expect(row).toHaveCount(1, { timeout: 60_000 })
		const image = row.locator("img[alt='big.png']")
		await expect(image).toBeVisible({ timeout: 30_000 })

		// В ленте — миниатюра: WebP, не больше 480 по большей стороне и заметно
		// легче оригинала. Место зарезервировано размерами оригинала.
		const src = (await image.getAttribute("src")) as string
		const thumb = await b.page.request.get(src)
		expect(thumb.status()).toBe(200)
		expect(thumb.headers()["content-type"]).toBe("image/webp")
		expect((await thumb.body()).length).toBeLessThan(original.length)
		await expect(image).toHaveAttribute("width", "1200")
		await expect(image).toHaveAttribute("height", "800")
		const natural = await image.evaluate((el: HTMLImageElement) => [el.naturalWidth, el.naturalHeight])
		expect(natural).toEqual([480, 320])

		// Оригинал — по ссылке вокруг картинки, байт в байт.
		const link = row.locator("a[data-attachment-original]")
		const full = await b.page.request.get((await link.getAttribute("href")) as string)
		expect(full.status()).toBe(200)
		expect(Buffer.compare(await full.body(), original), "оригинал не тронут").toBe(0)
	})

	test("ATT-003: файл (PDF) доходит с именем", async () => {
		const pdf = Buffer.from("%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")
		await pick(a, { name: "договор.pdf", mimeType: "application/pdf", buffer: pdf })
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "ready", { timeout: 60_000 })
		await a.page.locator("[data-composer-send]").click()

		await expect(b.page.getByText("договор.pdf").first()).toBeVisible({ timeout: 60_000 })
	})

	test("ATT-004: голосовое от A — длина видна до загрузки, B слушает и скачивает", async () => {
		await a.context.grantPermissions(["microphone"])
		const audio = b.page.locator("[data-attachment-audio]")
		const before = await audio.count()

		await a.page.locator("[data-composer-record]").click()
		await expect(a.page.locator("[data-recording-bar]")).toBeVisible()
		// Запись идёт настоящим временем: длительность считает рекордер по часам.
		await a.page.waitForTimeout(2500)
		await expect(a.page.locator("[data-recording-timer]")).not.toHaveText("0:00")
		await a.page.locator("[data-recording-stop]").click()

		// Длина записи видна в черновике сразу, в любом состоянии загрузки.
		const duration = a.page.locator("[data-attachment-duration]")
		await expect(duration).toBeVisible()
		await expect(duration).toHaveText(/^0:0[2-5]$/)
		await expect(draft(a)).toHaveAttribute("data-attachment-draft", "ready", { timeout: 60_000 })
		await a.page.locator("[data-composer-send]").click()

		await expect(audio).toHaveCount(before + 1, { timeout: 60_000 })
		// То же у отправителя: своё голосовое играется в ленте, а не ссылкой
		// в новую вкладку (ответ на отправку собирается другим путём, чем
		// публикация, и ломаться может только он).
		await expect(a.page.locator("[data-attachment-voice]").last().locator("audio")).toBeVisible()
		await expect(
			a.page.locator("[data-attachment-voice]").last().locator("a[data-attachment-link]"),
		).toHaveCount(0)
		const player = audio.last()
		const src = await player.getAttribute("src")
		expect(src, "у голосового есть ссылка на скачивание").toBeTruthy()
		const downloaded = await b.page.request.get(src as string)
		expect(downloaded.status()).toBe(200)
		const body = await downloaded.body()
		expect(body.length, "файл не пустой").toBeGreaterThan(1000)
		// WebM (EBML): то, что записал `MediaRecorder` Chromium, а не заглушка.
		expect(body.subarray(0, 4).toString("hex")).toBe("1a45dfa3")

		// Длина в ленте у получателя та же, что показал рекордер отправителю.
		const label = b.page.locator("[data-attachment-voice]").last()
		await expect(label).toContainText(/0:0[2-5]/)
		await expect(draft(a)).toHaveCount(0)

		// После перезагрузки история несёт то же: плеер в ленте, а не ссылка.
		await b.page.reload()
		await openConversation(b, conversationId)
		await expect(
			b.page.locator("[data-attachment-voice]").last().locator("audio"),
		).toBeVisible({ timeout: 30_000 })
		await expect(
			b.page.locator("[data-attachment-voice]").last().locator("a[data-attachment-link]"),
		).toHaveCount(0)
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
