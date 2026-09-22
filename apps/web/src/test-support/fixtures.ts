import type { Conversation, CurrentUser } from "../shared/lib/types";

/**
 * Общие входы `component`-детекторов среза 6.
 *
 * `component`-строки раздела F проверяют не отрисовку, а отсутствие в дереве
 * того, чего сервер не сообщал: подзаголовка о присутствии, кнопки без
 * обработчика, слова Active в счётчике. Каждому из этих тестов нужен
 * `Conversation`, и размноженный по файлам литерал разошёлся бы ровно там, где
 * в модель добавится поле, — то есть перестал бы проверять то, что проверял.
 */

/** Пользователь, каким его отдаёт адаптер `/me`: без `handle` и `presence`. */
export const VIEWER: CurrentUser = {
  name: "David Miller",
  email: "david.miller@example.com",
  emailVerified: true,
};

/**
 * `hasMessages: true` по умолчанию — не «так вероятнее», а чтобы умолчание
 * падало громче: беседа с сообщениями не имеет права показать «No messages
 * yet», и тест, забывший про `last_message`, обязан краснеть, а не зеленеть на
 * пустом превью.
 */
export function conversationOf(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "c1",
    name: "Anna Petrova",
    lastMessagePreview: "That works! Let's review the finalized slides tomorrow.",
    lastMessageTimestamp: "14:22",
    hasMessages: true,
    ...overrides,
  };
}
