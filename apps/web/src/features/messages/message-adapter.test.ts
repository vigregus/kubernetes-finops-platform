// Детекторы таблицы соответствия `Message` → `ChatMessage` (срез 4).
//
// Проверяется то, ради чего модуль существует отдельно: свои сообщения
// отличаются от чужих, вид сообщения берётся из типа, а время и размер
// становятся display-строками, а не уезжают в разметку как есть. Оба последних
// случая — ловушки компиляции: `timestamp: dto.createdAt` и
// `sizeLabel: String(dto.sizeBytes)` собираются без единой ошибки `tsc` и
// ломают разметку молча, поэтому детектор нужен именно на них.
//
// Часы и зона — параметры, а не момент и место прогона: иначе проверка
// «14:22» зеленела бы и краснела по календарю и по зоне машины с тестами.

import { describe, expect, it } from "vitest"

import type { Attachment as AttachmentDto, Message as MessageDto } from "../../api/generated"
import { adaptMessage, adaptMessages } from "./message-adapter"

const NOW = new Date("2026-09-20T18:00:00Z")
const EN = { locale: "en-US", timeZone: "UTC" } as const

const VIEWER = "user-viewer"
const ANNA = "user-anna"

function message(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    messageId: "m-1",
    conversationId: "c-1",
    seq: 1,
    senderId: ANNA,
    type: "text",
    createdAt: "2026-09-20T14:22:31Z",
    ...overrides,
  }
}

function attachment(overrides: Partial<AttachmentDto> = {}): AttachmentDto {
  return { attachmentId: "a-1", contentType: "image/png", sizeBytes: 1024, ...overrides }
}

function adapt(dto: MessageDto, viewer: string = VIEWER) {
  return adaptMessage(dto, viewer, NOW, EN)
}

describe("зритель видит себя", () => {
  it("своё сообщение помечено словом модели, чужое — идентификатором", () => {
    // Модель различает свои и чужие по `authorId === "me"`. Прокинутый
    // `sender_id` собрался бы без ошибки и показал бы собственное сообщение
    // как входящее.
    expect(adapt(message({ senderId: VIEWER })).authorId).toBe("me")
    expect(adapt(message({ senderId: ANNA })).authorId).toBe(ANNA)
  })

  it("имени отправителя не выдумывается: в ответе его нет", () => {
    // `Message` отдаёт только `sender_id`. Список участников — другой запрос,
    // которого G3-006 не делает.
    const adapted = adapt(message())

    expect(adapted.authorName).toBeUndefined()
    expect(adapted.avatarUrl).toBeUndefined()
  })
})

describe("вид сообщения", () => {
  it("текст остаётся текстом, вложение — вложением", () => {
    expect(adapt(message({ type: "text" })).kind).toBe("text")
    expect(adapt(message({ type: "image", attachments: [attachment()] })).kind).toBe("attachment")
    expect(adapt(message({ type: "file", attachments: [attachment()] })).kind).toBe("attachment")
    expect(adapt(message({ type: "voice", attachments: [attachment()] })).kind).toBe("attachment")
  })

  it("служебное сообщение — unsupported, а не чья-то реплика", () => {
    // Рендера служебных записей в интерфейсе нет ни одного. Показать такую
    // запись как текстовую значило бы выдать её содержимое за реплику
    // участника.
    expect(adapt(message({ type: "system", payload: { text: "Anna added Marcus" } })).kind).toBe(
      "unsupported",
    )
  })

  it("тип, которого интерфейс не знает, тоже unsupported", () => {
    expect(adapt(message({ type: "poll" as MessageDto["type"] })).kind).toBe("unsupported")
  })

  it("сообщение без payload даёт сообщение, а не падение", () => {
    // `payload` необязателен в контракте, и обращение к нему напрямую
    // (`dto.payload.text`) роняет адаптер на первом же таком сообщении.
    const adapted = adapt(message({ type: "text", payload: undefined }))

    expect(adapted.kind).toBe("text")
    expect(adapted.text).toBeUndefined()
    // Пузырь остаётся пустым, и это лучше подставного слова: «Message» в теле
    // сообщения выдавало бы себя за содержимое, которого сервер не присылал.
    expect(adapted.text).not.toBe("Message")
  })

  it("текст вложения-подписи не теряется", () => {
    const adapted = adapt(
      message({ type: "image", payload: { text: "look at this" }, attachments: [attachment()] }),
    )

    expect(adapted.text).toBe("look at this")
  })
})

describe("время — display-строка, а не ISO", () => {
  it("сегодняшнее сообщение показывает время", () => {
    // Прокинутый `created_at` дал бы в пузыре `2026-09-20T14:22:31Z`: типы
    // сходятся, `tsc` молчит, разметка испорчена.
    expect(adapt(message()).timestamp).toBe("14:22")
  })

  it("вчерашнее и старое называются словом и датой", () => {
    expect(adapt(message({ createdAt: "2026-09-19T14:22:31Z" })).timestamp).toBe("Yesterday")
    expect(adapt(message({ createdAt: "2026-09-02T14:22:31Z" })).timestamp).toBe("Sep 2")
  })

  it("неразбираемая отметка не превращается в «Invalid Date»", () => {
    // `Invalid Date` в ленте — утверждение о времени, которого сервер не
    // делал. Пустая строка — отсутствие времени.
    expect(adapt(message({ createdAt: "не дата" })).timestamp).toBe("")
  })
})

describe("вложение", () => {
  it("вид вложения читается из content_type", () => {
    const of = (contentType: string) =>
      adapt(message({ type: "file", attachments: [attachment({ contentType })] })).attachment?.kind

    expect(of("image/png")).toBe("image")
    expect(of("audio/ogg")).toBe("voice")
    expect(of("application/pdf")).toBe("file")
    expect(of("text/plain")).toBe("file")
  })

  it("имя — название вида, а не выдуманное имя файла", () => {
    // В `Attachment` нет ни `name`, ни `file_name` — подставляется вид.
    const name = (contentType: string) =>
      adapt(message({ type: "file", attachments: [attachment({ contentType })] })).attachment?.name

    expect(name("image/png")).toBe("Photo")
    expect(name("audio/ogg")).toBe("Voice message")
    expect(name("application/pdf")).toBe("File")
  })

  it("состояние — ready, и незавершённых состояний не выдумывается", () => {
    // Признака проверки файла в серверном `Attachment` нет вовсе; `processing`
    // нарисовало бы вечный спиннер «Checking file…» и утверждало бы проверку,
    // о которой сервер молчал.
    const adapted = adapt(message({ type: "file", attachments: [attachment()] }))

    expect(adapted.attachment?.state).toBe("ready")
  })

  it("размер — строка, а не число байт", () => {
    // `size_bytes` в поле `sizeLabel` дал бы «2516582» посреди человеческого
    // текста: типы сходятся, разметка испорчена.
    const size = (sizeBytes: number) =>
      adapt(message({ type: "file", attachments: [attachment({ sizeBytes })] })).attachment?.sizeLabel

    expect(size(2516582)).toBe("2.4 MB")
    expect(size(1024)).toBe("1 KB")
    expect(size(512)).toBe("512 B")
  })

  it("длительность голосового переводится в секунды", () => {
    const adapted = adapt(
      message({
        type: "voice",
        payload: { durationMs: 14800 },
        attachments: [attachment({ contentType: "audio/ogg" })],
      }),
    )

    expect(adapted.attachment?.durationSeconds).toBe(14)
  })

  it("ссылка на файл попадает в модель как есть", () => {
    const adapted = adapt(
      message({ type: "file", attachments: [attachment({ downloadUrl: "https://cdn/x" })] }),
    )

    expect(adapted.attachment?.url).toBe("https://cdn/x")
  })

  it("сообщение вложения без вложения остаётся вложением, но без объекта", () => {
    // Вид берётся из типа сообщения — утверждения сервера, — а не из наличия
    // массива: понижение до текста показало бы картинку пустой репликой.
    const adapted = adapt(message({ type: "image", attachments: undefined }))

    expect(adapted.kind).toBe("attachment")
    expect(adapted.attachment).toBeUndefined()
  })
})

describe("надгробие и квитанции", () => {
  it("deleted_at делает сообщение удалённым", () => {
    expect(adapt(message({ deletedAt: "2026-09-20T15:00:00Z" })).deleted).toBe(true)
    expect(adapt(message()).deleted).toBe(false)
  })

  it("состояние доставки не выводится из того, что сообщение лежит в истории", () => {
    expect(adapt(message()).deliveryState).toBeUndefined()
  })
})

describe("страница", () => {
  it("порядок ответа не меняется: страница — это список записей, а не лента", () => {
    // Сортировать здесь значило бы завести второе место порядка: лента
    // сортируется в `eventMerge`, и туда страница обязана приехать как есть.
    const items = adaptMessages(
      [message({ messageId: "m-103", seq: 103 }), message({ messageId: "m-102", seq: 102 })],
      VIEWER,
      NOW,
      EN,
    )

    expect(items.map((it) => it.seq)).toEqual([103, 102])
  })
})
