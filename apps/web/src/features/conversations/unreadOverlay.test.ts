// Детекторы числа непрочитанного: разбор события личного канала и наложение
// его на список бесед.
//
// Оба свойства — предмет мутаций среза 5: (а) событие несёт **абсолютное**
// число, а не приращение; (б) отсутствие ключа в событии не превращается в ноль.
// Оба проверяются здесь, без браузера, потому что правило принадлежит модулю,
// а не компоненту.

import { describe, expect, it } from "vitest"

import type { Conversation } from "../../shared/lib/types"
import { adaptUnreadChanged, withUnreadOverlay } from "./unreadOverlay"

const CONVERSATION_A = "3f6b0d1e-0f4e-4a1f-9d2b-3ad0d1e6a111"
const CONVERSATION_B = "8c1f2a34-5b6d-4e7f-8a90-1b2c3d4e5f60"

function conversation(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    name: "Anna Petrova",
    lastMessagePreview: "See you tomorrow",
    hasMessages: true,
    ...overrides,
  }
}

describe("разбор unread.changed", () => {
  it("событие с обоими полями разбирается в пару «беседа, число»", () => {
    expect(
      adaptUnreadChanged({
        type: "unread.changed",
        conversation_id: CONVERSATION_A,
        unread_count: 3,
      }),
    ).toEqual({ conversationId: CONVERSATION_A, unreadCount: 3 })
  })

  it("ноль — законное число, а не отсутствие", () => {
    // «Прочитано всё» — настоящее утверждение сервера, и оно обязано доехать:
    // иначе счётчик на экране остался бы прежним там, где человек всё прочёл.
    expect(
      adaptUnreadChanged({
        type: "unread.changed",
        conversation_id: CONVERSATION_A,
        unread_count: 0,
      }),
    ).toEqual({ conversationId: CONVERSATION_A, unreadCount: 0 })
  })

  it("событие без числа отбрасывается, а не читается нулём", () => {
    // Подстановка нуля здесь — не мелочь: `0` означает «непрочитанного нет»,
    // то есть утверждение, которого сервер не делал, и оно откатило бы счётчик
    // на экране. `null` означает «события нет», и это разные вещи.
    expect(adaptUnreadChanged({ type: "unread.changed", conversation_id: CONVERSATION_A })).toBeNull()
  })

  it("событие без беседы отбрасывается", () => {
    expect(adaptUnreadChanged({ type: "unread.changed", unread_count: 3 })).toBeNull()
  })

  it("пустой идентификатор беседы — выдуманное значение, а не отсутствие", () => {
    expect(adaptUnreadChanged({ type: "unread.changed", conversation_id: "", unread_count: 3 })).toBeNull()
  })

  it("дробное и отрицательное число не принимаются", () => {
    expect(adaptUnreadChanged({ type: "unread.changed", conversation_id: CONVERSATION_A, unread_count: -1 })).toBeNull()
    expect(adaptUnreadChanged({ type: "unread.changed", conversation_id: CONVERSATION_A, unread_count: 2.5 })).toBeNull()
  })

  it("чужой тип события в список не едет", () => {
    // Тем же каналом может прийти другое событие (`session.revoked` —
    // `userEvent.type`), и приписать его счётчику значило бы показать число
    // из события не о том.
    expect(
      adaptUnreadChanged({ type: "session.revoked", conversation_id: CONVERSATION_A, unread_count: 3 }),
    ).toBeNull()
  })

  it("незнакомое поле событие не отбрасывает", () => {
    // `CTR-003`: событие будущей версии с лишним ключом обязано приниматься
    // старым клиентом — `additionalProperties` в схеме нет намеренно. Отказ
    // из-за незнакомого ключа превратил бы безобидное расширение в
    // пропущенное событие, и это дефект совместимости, а не осторожность.
    expect(
      adaptUnreadChanged({
        type: "unread.changed",
        conversation_id: CONVERSATION_A,
        unread_count: 3,
        read_at: "2026-09-25T10:00:00Z",
      }),
    ).toEqual({ conversationId: CONVERSATION_A, unreadCount: 3 })
  })

  it("не объект — не событие", () => {
    expect(adaptUnreadChanged(null)).toBeNull()
    expect(adaptUnreadChanged("unread.changed")).toBeNull()
  })
})

describe("наложение оверлея на список", () => {
  it("число из оверлея заменяет базовое, а не складывается с ним", () => {
    // Событие несёт **абсолютное** число. Сложение сдвинуло бы счётчик вверх
    // на каждом повторе доставки (пачка Kafka приезжает второй раз), и
    // исправить это на клиенте нечем.
    const base = [conversation(CONVERSATION_A, { unreadCount: 5 })]

    expect(withUnreadOverlay(base, new Map([[CONVERSATION_A, 2]]))).toEqual([
      conversation(CONVERSATION_A, { unreadCount: 2 }),
    ])
  })

  it("беседа без записи в оверлее остаётся как есть", () => {
    const base = [conversation(CONVERSATION_A, { unreadCount: 5 }), conversation(CONVERSATION_B)]

    expect(withUnreadOverlay(base, new Map([[CONVERSATION_A, 2]]))).toEqual([
      conversation(CONVERSATION_A, { unreadCount: 2 }),
      conversation(CONVERSATION_B),
    ])
  })

  it("отсутствие ключа в оверлее не превращается в ноль", () => {
    // У беседы без `unreadCount` его и не появляется: «сервер числа не назвал»
    // и «всё прочитано» — разные состояния, и оверлей не имеет права делать
    // их одним.
    const base = [conversation(CONVERSATION_B)]

    const [result] = withUnreadOverlay(base, new Map([[CONVERSATION_A, 0]]))

    expect(result).toEqual(conversation(CONVERSATION_B))
    expect(result.unreadCount).toBeUndefined()
  })

  it("беседа оверлея, которой нет в списке, пропускается", () => {
    // Показать её нечем, а завести беседу из одного числа значило бы выдумать
    // содержимое. Это не потеря факта: список придёт со следующей сверкой.
    const base = [conversation(CONVERSATION_A)]

    expect(withUnreadOverlay(base, new Map([[CONVERSATION_B, 7]]))).toEqual([conversation(CONVERSATION_A)])
  })

  it("пустой оверлей не трогает список", () => {
    const base = [conversation(CONVERSATION_A, { unreadCount: 5 })]

    expect(withUnreadOverlay(base, new Map())).toEqual(base)
  })
})
