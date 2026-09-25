import { describe, expect, it } from "vitest"
import { ConversationListPageFromJSON } from "../../api/generated"
import type { Conversation as ConversationDto, Message as MessageDto, UserSummary } from "../../api/generated"
import { adaptConversation, adaptConversations } from "./adapter"

/**
 * Часы и зона — параметры, а не момент и место прогона: иначе проверка
 * «14:22 / Yesterday» зеленела бы и краснела по календарю и по тому, в какой
 * зоне оказалась машина с тестами.
 */
const NOW = new Date("2026-09-20T18:00:00Z")
const EN = { locale: "en-US", timeZone: "UTC" } as const

const VIEWER = "user-viewer"
const ANNA = "user-anna"
const MARCUS = "user-marcus"

function person(userId: string, displayName: string, overrides: Partial<UserSummary> = {}): UserSummary {
  return { userId, displayName, ...overrides }
}

const ANNA_SUMMARY = person(ANNA, "Anna Petrova", { lastSeenAt: "2026-09-20T16:20:00Z" })
const MARCUS_SUMMARY = person(MARCUS, "Marcus Chen")

function message(overrides: Partial<MessageDto> = {}): MessageDto {
  return {
    messageId: "m1",
    conversationId: "c1",
    seq: 1,
    senderId: ANNA,
    type: "text",
    createdAt: "2026-09-20T14:22:31Z",
    ...overrides,
  }
}

function conversation(overrides: Partial<ConversationDto> = {}): ConversationDto {
  return {
    conversationId: "c1",
    // Зритель стоит первым — так его и вернёт подзапрос участников с
    // `ORDER BY cm2.joined_at, cm2.user_id`: он вошёл в беседу раньше.
    participants: [person(VIEWER, "David Miller"), ANNA_SUMMARY],
    type: "direct",
    createdAt: "2026-09-01T00:00:00Z",
    ...overrides,
  }
}

function adapt(dto: ConversationDto, viewer: string = VIEWER) {
  return adaptConversation(dto, viewer, NOW, EN)
}

describe("таблица соответствия /conversations", () => {
  it("для личной беседы имя берётся у собеседника, а не у первого в списке", () => {
    // Подстановка `participants[0]` дала бы «David Miller» — зритель показан
    // самому себе под именем собеседника.
    expect(adapt(conversation()).name).toBe("Anna Petrova")
  })

  it("для группы имя собирается из всех остальных", () => {
    const result = adapt(
      conversation({ type: "group", participants: [person(VIEWER, "David Miller"), ANNA_SUMMARY, MARCUS_SUMMARY] }),
    )

    expect(result.name).toBe("Anna Petrova, Marcus Chen")
    // Одна отметка времени на нескольких человек смысла не имеет: поданная как
    // факт, она была бы выдумкой.
    expect(result.lastSeenAt).toBeUndefined()
  })

  it("группа без других участников получает непустое нейтральное имя", () => {
    const result = adapt(conversation({ type: "group", participants: [person(VIEWER, "David Miller")] }))

    // Пустая строка здесь — не косметика: `Avatar` считает инициалы из `name`
    // (`shared/ui/Avatar.tsx:21`) и на пустом имени даёт пустую подпись.
    expect(result.name).toBe("Group")
  })

  it("отсутствие unread_count — это «неизвестно», а не ноль", () => {
    expect(adapt(conversation()).unreadCount).toBeUndefined()
    // А явный ноль сервера остаётся нулём: «всё прочитано» — тоже факт.
    expect(adapt(conversation({ unreadCount: 0 })).unreadCount).toBe(0)
  })

  it("удалённое последнее сообщение показывается надгробием, а не своим текстом", () => {
    const result = adapt(
      conversation({
        lastMessage: message({ deletedAt: "2026-09-20T14:30:00Z", payload: { text: "встречаемся в 15:00" } }),
      }),
    )

    // `payload` у надгробия может уцелеть: показать его — значит показать то,
    // что человек удалил.
    expect(result.lastMessagePreview).toBe("Message deleted")
    expect(result.previewDeleted).toBe(true)
  })

  it("превью для типов без текста — по таблице, без падения на отсутствующем payload", () => {
    const cases: Array<[MessageDto["type"], string]> = [
      ["image", "Photo"],
      ["file", "File"],
      ["voice", "Voice message"],
    ]

    for (const [type, expected] of cases) {
      expect(adapt(conversation({ lastMessage: message({ type }) })).lastMessagePreview, `тип ${type}`).toBe(expected)
    }
  })

  it("текстовое сообщение без payload и неизвестный тип дают нейтральное превью", () => {
    expect(adapt(conversation({ lastMessage: message({ type: "text", payload: {} }) })).lastMessagePreview).toBe(
      "Message",
    )
    expect(adapt(conversation({ lastMessage: message({ type: "system" }) })).lastMessagePreview).toBe("Message")
  })

  it("беседа без last_message не выдумывает ни превью, ни времени", () => {
    const result = adapt(conversation())

    // Пустая строка кормит существующую ветку `isEmpty` компонента
    // (`ConversationListItem.tsx:26`), а не второй текст для того же состояния.
    expect(result.lastMessagePreview).toBe("")
    expect(result.previewDeleted).toBe(false)
    expect(result.lastMessageTimestamp).toBeUndefined()
  })

  it("время последнего сообщения — display-строка, а не ISO из ответа", () => {
    expect(adapt(conversation({ lastMessage: message() })).lastMessageTimestamp).toBe("14:22")
    expect(
      adapt(conversation({ lastMessage: message({ createdAt: "2026-09-19T09:14:00Z" }) })).lastMessageTimestamp,
    ).toBe("Yesterday")
  })

  it("отметка собеседника доезжает, а её отсутствие — отсутствие, а не null", () => {
    expect(adapt(conversation()).lastSeenAt).toBe("2026-09-20T16:20:00Z")

    const withoutMark = adapt(
      conversation({
        participants: [person(VIEWER, "David Miller"), person(ANNA, "Anna Petrova", { lastSeenAt: null })],
      }),
    )
    expect(withoutMark.lastSeenAt).toBeUndefined()
  })

  it("online доезжает словом: true — Online, false — Offline, а не Away", () => {
    // `false` — положительное утверждение сервера («подтверждённой активности
    // не было окно»), и потому у него есть слово. Но слово это `offline`:
    // `away` обещало бы «в сети, но отошёл» — состояние, которого сервер не
    // сообщал вовсе.
    const online = adapt(conversation({ participants: [person(VIEWER, "David Miller"), person(ANNA, "Anna", { online: true })] }))
    expect(online.presence).toBe("online")

    const offline = adapt(
      conversation({ participants: [person(VIEWER, "David Miller"), person(ANNA, "Anna", { online: false })] }),
    )
    expect(offline.presence).toBe("offline")
  })

  it("ключа online нет — присутствия нет, и это не «офлайн»", () => {
    // Старый сервер и любой ответ без ключа дают `undefined`, и свёртка в два
    // исхода (`online ? "online" : "away"`) показала бы здесь «Away» — то есть
    // утверждение о человеке, которого сервер не делал. Отметка при этом на
    // месте: два ответа о человеке не заменяют друг друга.
    const result = adapt(
      conversation({
        participants: [person(VIEWER, "David Miller"), person(ANNA, "Anna", { lastSeenAt: "2026-09-20T16:20:00Z" })],
      }),
    )

    expect(result.presence).toBeUndefined()
    expect(result.lastSeenAt).toBe("2026-09-20T16:20:00Z")
  })

  it("группе присутствие не положено — как и отметка времени", () => {
    const result = adapt(
      conversation({
        type: "group",
        participants: [
          person(VIEWER, "David Miller"),
          person(ANNA, "Anna", { online: true }),
          person(MARCUS, "Marcus", { online: true }),
        ],
      }),
    )

    expect(result.presence).toBeUndefined()
  })

  it("наличие последнего сообщения — признак, а не вывод из пустоты превью", () => {
    // Главная панель говорит «No messages yet» только тогда, когда сообщений
    // действительно нет. Выводить это из `lastMessagePreview === ""` значило бы
    // держать утверждение на договорённости таблицы превью «пустая строка
    // бывает только у беседы без сообщений» — то есть на выводе, а не на факте,
    // который сервер сообщил ключом `last_message`.
    expect(adapt(conversation({ lastMessage: message() })).hasMessages).toBe(true)
    expect(adapt(conversation()).hasMessages).toBe(false)
  })

  it("страница целиком адаптируется одним и тем же зрителем", () => {
    // Вход задан **по проводу**, в snake_case: `ConversationListPageFromJSON`
    // читает `display_name` и `user_id`, и подмена регистра дала бы здесь
    // «неразбираемое имя», то есть тест проверял бы собственную ошибку.
    const page = ConversationListPageFromJSON({
      items: [
        {
          conversation_id: "c1",
          type: "direct",
          participants: [
            { user_id: VIEWER, display_name: "David Miller" },
            { user_id: ANNA, display_name: "Anna Petrova" },
          ],
          created_at: "2026-09-01T00:00:00Z",
        },
        {
          conversation_id: "c2",
          type: "group",
          participants: [
            { user_id: VIEWER, display_name: "David Miller" },
            { user_id: MARCUS, display_name: "Marcus Chen" },
          ],
          created_at: "2026-09-02T00:00:00Z",
        },
      ],
      next_before_activity_at: null,
      next_before_conversation_id: null,
    })

    // Зритель один на всю страницу: адаптация идёт после `/me`, а не до него.
    expect(adaptConversations(page, VIEWER, NOW, EN).map((item) => item.name)).toEqual([
      "Anna Petrova",
      "Marcus Chen",
    ])
  })

  it("модель не несёт полей, о которых сервер молчит", () => {
    // Точный состав, а не «нет чего-то конкретного»: набор печатающих, аватар
    // и блокировки источника не имеют, и появление любого из них в модели —
    // это утверждение, которого сервер не делал.
    //
    // `presence` в списке — не исключение из правила, а его исполнение: поле
    // заполняется из `online` и остаётся пустым, когда ключа нет (проверено
    // выше). Блокировка сюда не попадает по другой причине: маска приходит
    // **отсутствием** значения, а не флагом, и поля `blocked*` контракт не
    // объявляет вовсе.
    expect(Object.keys(adapt(conversation({ lastMessage: message() }))).sort()).toEqual([
      "hasMessages",
      "id",
      "lastMessagePreview",
      "lastMessageTimestamp",
      "lastSeenAt",
      "name",
      "presence",
      "previewDeleted",
      "unreadCount",
    ])
  })
})
