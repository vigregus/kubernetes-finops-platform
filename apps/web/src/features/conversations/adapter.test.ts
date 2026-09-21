import { describe, expect, it } from "vitest"
import type { Conversation as ConversationDto, Message as MessageDto, UserSummary } from "../../api/generated"
import { adaptConversation } from "./adapter"

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

function person(userId: string, displayName: string, lastSeenAt?: string | null): UserSummary {
  return { userId, displayName, lastSeenAt }
}

const ANNA_SUMMARY = person(ANNA, "Anna Petrova", "2026-09-20T16:20:00Z")
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
      conversation({ participants: [person(VIEWER, "David Miller"), person(ANNA, "Anna Petrova", null)] }),
    )
    expect(withoutMark.lastSeenAt).toBeUndefined()
  })

  it("модель не несёт полей, о которых сервер молчит", () => {
    // Точный состав, а не «нет чего-то конкретного»: присутствие, набор
    // печатающих, блокировки и аватар источника не имеют, и появление любого
    // из них в модели — это утверждение, которого сервер не делал.
    expect(Object.keys(adapt(conversation({ lastMessage: message() }))).sort()).toEqual([
      "id",
      "lastMessagePreview",
      "lastMessageTimestamp",
      "lastSeenAt",
      "name",
      "previewDeleted",
      "unreadCount",
    ])
  })
})
