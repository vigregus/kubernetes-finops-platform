// Утверждения о сгенерированном клиенте (CTR-004).
//
// Файл отвечает на один вопрос: клиент, лежащий в `api/generated/`, — тот
// самый, на который рассчитывает остальной код. Контракт правится отдельно
// от фронта, и без этих утверждений расхождение обнаружилось бы в браузере:
// в клиенте нет операции, которую зовёт обёртка, или поле приходит не тем
// типом, на который типизирован адаптер.
//
// Красный прогон здесь приходит от `tsc -b`, а не от `vitest run`. Это
// сказано явно, чтобы его не искали в выводе тестов и не объявляли
// отсутствующим: утверждения типовые, их исполняет `build:app`, который
// в `make web-check` идёт раньше тестов. Часть про рантайм (ниже) исполняет
// vitest.
//
// Ожидаемая форма шести полей — не предположение, а измерение закреплённого
// digest'а генератора. Три из шести могли бы выйти иначе, и тогда решение
// принималось бы здесь же, а не в UI.

import { describe, expect, it } from "vitest"

import { AuthApi, ConversationsApi, MeFromJSON, UserSummaryFromJSON } from "./generated"
import type { Conversation, ConversationListPage, Me, Message, UserSummary } from "./generated"

// --- Обязательный набор операций -------------------------------------------
//
// Не закрытый список операций контракта: шестнадцатая совместимая операция
// ронять фронт не должна, а исчезнувшая или переименованная — обязана.
// Ссылка на метод падает ещё на `tsc`: «Property 'listConversations' does
// not exist on type 'ConversationsApi'».

export const REQUIRED_OPERATIONS = {
  exchangeAuthorizationCode: AuthApi.prototype.exchangeAuthorizationCode,
  refreshAccessToken: AuthApi.prototype.refreshAccessToken,
  whoAmI: AuthApi.prototype.whoAmI,
  listConversations: ConversationsApi.prototype.listConversations,
}

// --- Форма сгенерированных типов -------------------------------------------

type Equal<A, B> = (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false
type Assert<T extends true> = T

// `created_at` — строка, а не `Date`. Снятый `typeMappings` в
// openapi-generator.yaml роняет именно это утверждение, и вслед за ним —
// `formatConversationTimestamp`, который собирает display-строку из строки.
export type MessageCreatedAtIsString = Assert<Equal<Message["createdAt"], string>>

// `Me.email` в контракте объявлен по-3.0 (`nullable: true`) в документе 3.1,
// где такого ключа нет. Генератор аннотацию не видит и `null` в тип не
// пускает — и это, вопреки ожиданию, верно: `MeFromJSON` сворачивает `null`
// в `undefined` сам (проверено ниже), поэтому объект никогда не несёт `null`,
// и тип, допускающий его, описывал бы состояние, которого не бывает.
export type MeEmailIsStringOrUndefined = Assert<Equal<Me["email"], string | undefined>>

// `last_seen_at` объявлен по-3.1 (`type: [string, 'null']`), и `undefined`
// здесь — не то же самое, что `null`: ключа нет у того, кто не был в сети
// ни разу, и это отсутствие отметки, а не отметка. Слитые в одно значение,
// они дали бы «ни разу не был» и «отметка неизвестна» под одним видом.
export type LastSeenAtKeepsNullDistinct = Assert<Equal<UserSummary["lastSeenAt"], string | null | undefined>>

// Последнее сообщение необязательно: беседа без сообщений — обычное
// состояние, а не ошибка. Обязательное поле заставило бы выдумать значение.
export type LastMessageIsOptional = Assert<Equal<Conversation["lastMessage"], Message | undefined>>

// `unread_count` не бывает `null`: отсутствие ключа означает «неизвестно»,
// а ноль — уверенное «всё прочитано». Второе представление для первого
// состояния дало бы `null` там, где адаптер ожидает `undefined`.
export type UnreadCountHasNoNull = Assert<Equal<Conversation["unreadCount"], number | undefined>>

// Оба поля курсора **обязательны** и присутствуют всегда, включая `null`:
// конец списка — это `null` в обоих, а не отсутствие ключа. Необязательное
// поле заставило бы клиент проверять наличие ключа там, где протокол обещает
// значение, — то есть отличать конец списка от отсутствия поля.
export type CursorIsRequiredAndNullable = Assert<Equal<ConversationListPage["nextBeforeActivityAt"], string | null>>

describe("обязательный набор операций", () => {
  it.each(Object.keys(REQUIRED_OPERATIONS))("%s присутствует", (name) => {
    expect(typeof REQUIRED_OPERATIONS[name as keyof typeof REQUIRED_OPERATIONS]).toBe("function")
  })
})

describe("рантайм не производит null там, где тип его не допускает", () => {
  // Это не повторение утверждения выше, а его основание: тип без `null`
  // честен ровно потому, что десериализатор сам приводит `null` к
  // `undefined`. Проверка идёт через настоящий `FromJSON`, а не через
  // приведение типа: приведение проверяло бы приведение.

  it("Me.email: null в теле ответа становится undefined", () => {
    const me = MeFromJSON({
      user_id: "11111111-1111-1111-1111-111111111111",
      display_name: "Fixture",
      email: null,
      email_verified: true,
      capabilities: ["read"],
    })

    expect(me.email).toBeUndefined()
  })

  it("UserSummary.last_seen_at: null тоже сворачивается в undefined", () => {
    const summary = UserSummaryFromJSON({
      user_id: "11111111-1111-1111-1111-111111111111",
      display_name: "Fixture",
      last_seen_at: null,
    })

    expect(summary.lastSeenAt).toBeUndefined()
  })
})
