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
import { adaptMessage, adaptMessages, adaptPublication, adaptReadReceipt } from "./message-adapter"

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

// Публикация канала (срез 5) — второй вход в ту же таблицу соответствия, и
// форма у него другая: измеренная (`_client_event`,
// `services/realtime_delivery.py:43-56`) — ровно пять полей, из которых контракт
// канала обязательным объявляет **только `type`** (`channels.json`,
// `$defs.conversationEvent`). Отсюда оба требования к разбору: он защитный (мусор
// и чужие события дают `null`, а не падение внутри обработчика публикации) и он
// ничего не выдумывает (времени отправки на проводе нет вовсе).

/** Публикация в измеренной форме: пять полей `_client_event`. */
type Publication = Record<string, unknown>

function publication(overrides: Publication = {}): Publication {
  return {
    type: "message.created",
    message_id: "m-1",
    seq: 1,
    sender_id: ANNA,
    payload: { text: "#1" },
    ...overrides,
  }
}

function fromChannel(overrides: Publication = {}, viewer: string = VIEWER) {
  return adaptPublication(publication(overrides), viewer)
}

describe("публикация канала", () => {
  it("живое сообщение становится моделью: опознание, номер, тело", () => {
    expect(fromChannel()).toMatchObject({ id: "m-1", seq: 1, text: "#1", kind: "text" })
  })

  it("своё сообщение узнаётся по идентификатору зрителя, а не по каналу", () => {
    // Тот же вопрос, что и у истории, и ответ обязан быть тот же: разошедшись,
    // эти два пути показали бы свои сообщения входящими ровно на одном из них.
    expect(fromChannel({ sender_id: VIEWER })?.authorId).toBe("me")
    expect(fromChannel({ sender_id: ANNA })?.authorId).toBe(ANNA)
  })

  it("квитанция и надгробие — не сообщения", () => {
    // Канал несёт три вида событий, а сообщение из них одно. Нарисовать
    // `message.read` репликой значило бы показать чужое событие как текст.
    expect(fromChannel({ type: "message.read" })).toBeNull()
    expect(fromChannel({ type: "message.deleted" })).toBeNull()
    // И незнакомый вид — тоже `null`, а не «наверное сообщение».
    expect(fromChannel({ type: "message.edited" })).toBeNull()
  })

  it("запись без номера не применяется", () => {
    // Номер — это и порядок, и признак пропуска, то есть предмет всего гейта.
    // `NaN` в `seq` положил бы слияние, а дробный номер не совпал бы ни с одной
    // границей: `2.5 > 1 + 1` — это «пропуск», которого нет.
    expect(fromChannel({ seq: undefined })).toBeNull()
    expect(fromChannel({ seq: "7" })).toBeNull()
    expect(fromChannel({ seq: 1.5 })).toBeNull()
    expect(fromChannel({ seq: 0 })).toBeNull()
  })

  it("запись без опознания и без отправителя не применяется", () => {
    // Обе — поля домена: опознание доказывает «ровно один раз» (RT-004), а
    // отправитель отличает свою реплику от чужой. Пустая строка на месте
    // любого из них была бы выдуманным значением, а не отсутствием.
    expect(fromChannel({ message_id: undefined })).toBeNull()
    expect(fromChannel({ message_id: "" })).toBeNull()
    expect(fromChannel({ sender_id: undefined })).toBeNull()
    expect(fromChannel({ sender_id: "" })).toBeNull()
  })

  it("время отправки не подменяется временем получения", () => {
    // На провод не уезжает ни `created_at`, ни иное время — измерено у
    // `_client_event`. Подставь сюда момент получения, и он поедет в пузырь как
    // время отправки: чем свежее сообщение, тем правдоподобнее подмена.
    expect(fromChannel()?.timestamp).toBe("")
  })

  it("вложение из публикации не выдумывается", () => {
    // `attachments` живут в ответе REST, в канал не попадают. Вложение,
    // отправленное живьём, доедет подписью и без вложения — это названная
    // граница канала, и её закрывает G3-007.
    expect(fromChannel()?.attachment).toBeUndefined()
    expect(fromChannel()?.kind).toBe("text")
  })

  it("тело не выдумывается, когда payload его не несёт", () => {
    // `payload` необязателен, а `text` в нём — не обязательно строка.
    expect(fromChannel({ payload: undefined })?.text).toBeUndefined()
    expect(fromChannel({ payload: {} })?.text).toBeUndefined()
    expect(fromChannel({ payload: { text: 42 } })?.text).toBeUndefined()
  })

  it("разбор защитный: мусор не роняет обработчик публикации", () => {
    // Падение здесь унесло бы соединение — то, ради чего гейт и существует.
    expect(adaptPublication(null, VIEWER)).toBeNull()
    expect(adaptPublication(undefined, VIEWER)).toBeNull()
    expect(adaptPublication("message.created", VIEWER)).toBeNull()
    expect(adaptPublication({}, VIEWER)).toBeNull()
    // Объект на месте `payload` — тоже допустимый провод.
    expect(fromChannel({ payload: "not an object" })?.text).toBeUndefined()
  })
})

// Разбор квитанции (D12) — своё правило, а не побочный эффект предыдущего.
// У события три необязательных поля, и «схема разрешает» здесь не то же, что
// «имеет смысл»: у неполного события и у расширенного будущего поля судьбы
// **противоположны**, и перепутать их — значит либо выдумать ноль, либо
// выбросить валидное событие.

/** Тело `message.read` в измеренной форме: `reader_id`, `read_seq`, `delivered_seq`. */
type ReceiptEvent = Record<string, unknown>

function receipt(overrides: ReceiptEvent = {}): ReceiptEvent {
  return {
    type: "message.read",
    reader_id: ANNA,
    read_seq: 5,
    delivered_seq: 7,
    ...overrides,
  }
}

describe("разбор квитанции собеседника", () => {
  it("квитанция становится моделью: читатель и оба числа", () => {
    expect(adaptReadReceipt(receipt())).toStrictEqual({
      readerId: ANNA,
      readSeq: 5,
      deliveredSeq: 7,
    })
  })

  it("одно поле из двух — законное тело, и уезжает ровно оно", () => {
    // `anyOf` в запросе (и то же в событии) — не лазейка: доставлено сообщает
    // устройство, прочитано — взгляд, и второго может не быть в том же круге.
    expect(adaptReadReceipt(receipt({ delivered_seq: undefined }))).toStrictEqual({
      readerId: ANNA,
      readSeq: 5,
    })
  })

  it("лишнее поле событие не отбрасывает: расширение сервера — не мусор", () => {
    // Это `CTR-003` в одну строку. Схема канала разрешает
    // `{"type":"message.read"}`, а `additionalProperties` в ней нет, поэтому
    // событие будущей версии с `read_at` обязано **приниматься**, а незнакомый
    // ключ — игнорироваться. Отбросить событие из-за незнакомого ключа значило
    // бы превратить безобидное расширение в пропущенное прочтение.
    const future = receipt({ read_at: "2026-09-20T15:00:00Z", device: { os: "ios" } })

    expect(adaptReadReceipt(future)).toStrictEqual({
      readerId: ANNA,
      readSeq: 5,
      deliveredSeq: 7,
    })
  })

  it("неполное событие — тишина, а не ноль", () => {
    // Все три поля необязательные — этого требует аддитивность, — значит
    // `{"type":"message.read"}` схемой разрешено. Разрешать не то же, что
    // принимать: `0` — уверенное «прочитано ни до чего», то есть утверждение о
    // собеседнике, которого он не делал, и оно откатило бы отметку на экране.
    expect(adaptReadReceipt(receipt({ read_seq: undefined, delivered_seq: undefined }))).toBeNull()
  })

  it("читатель без имени не принимается", () => {
    expect(adaptReadReceipt(receipt({ reader_id: undefined }))).toBeNull()
    expect(adaptReadReceipt(receipt({ reader_id: "" }))).toBeNull()
    expect(adaptReadReceipt(receipt({ reader_id: 7 }))).toBeNull()
  })

  it("номер вне целых неотрицательных не становится нулём", () => {
    expect(adaptReadReceipt(receipt({ read_seq: "5" }))).toBeNull()
    expect(adaptReadReceipt(receipt({ read_seq: 2.5 }))).toBeNull()
    expect(adaptReadReceipt(receipt({ read_seq: -1 }))).toBeNull()
    expect(adaptReadReceipt(receipt({ read_seq: Number.NaN }))).toBeNull()
    expect(adaptReadReceipt(receipt({ delivered_seq: "7" }))).toBeNull()
  })

  it("ноль при этом законен: это утверждение, а не молчание", () => {
    // Оба поля запроса разрешают `minimum: 0`, и квитанция «прочитано до нуля»
    // — такое же известие, как любое другое. Отличить его от отсутствия поля
    // обязан разбор, а не получатель.
    expect(adaptReadReceipt(receipt({ read_seq: 0, delivered_seq: 0 }))).toStrictEqual({
      readerId: ANNA,
      readSeq: 0,
      deliveredSeq: 0,
    })
  })

  it("чужое событие квитанцией не становится", () => {
    // Имя типа — часть утверждения: без него `reader_id` с числами неотличим от
    // события другого вида, которое мы просто не знаем.
    expect(adaptReadReceipt(publication())).toBeNull()
    expect(adaptReadReceipt({ reader_id: ANNA, read_seq: 5, delivered_seq: 7 })).toBeNull()
    expect(adaptReadReceipt({ type: "message.deleted", reader_id: ANNA, read_seq: 5 })).toBeNull()
  })

  it("разбор защитный: мусор не роняет обработчик публикации", () => {
    expect(adaptReadReceipt(null)).toBeNull()
    expect(adaptReadReceipt(undefined)).toBeNull()
    expect(adaptReadReceipt("message.read")).toBeNull()
    expect(adaptReadReceipt([])).toBeNull()
  })
})
