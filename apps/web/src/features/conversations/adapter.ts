/**
 * Таблица соответствия `GET /conversations` → модель списка бесед.
 *
 * Два правила, ради которых этот файл существует отдельно от компонента.
 *
 * **Зритель видит не себя.** Запрос отдаёт участников **включая самого
 * зрителя**: подзапрос собирает всех действующих участников беседы
 * (`repositories/conversations.py:87-95`), а внешний отбор ограничивает лишь
 * то, чьи беседы мы читаем (`WHERE cm.user_id = $1`). Поэтому «собеседник» —
 * это участник, чей `user_id` не равен `currentUserId`, а не `participants[0]`:
 * для личной беседы участников двое, и «первый в списке» показал бы человеку
 * его же имя.
 *
 * **Чего сервер не сказал — того интерфейс не утверждает.** Отсюда отсутствие
 * `presence` (сервер не отдаёт онлайн вовсе), отсутствие `unreadCount` при
 * отсутствии ключа («неизвестно» — не то же самое, что «всё прочитано»),
 * необязательное `lastMessageTimestamp` и пустое превью у беседы без сообщений.
 */

import type { Conversation as ConversationDto, Message as MessageDto, UserSummary } from "../../api/generated"
import type { Conversation } from "../../shared/lib/types"
import { formatConversationTimestamp, type TimestampFormatOptions } from "./formatTimestamp"

/**
 * Нейтральное непустое имя, когда имён не осталось.
 *
 * По-английски, как и весь остальной интерфейс: русская строка посреди
 * англоязычного списка была бы скрытой продуктовой переделкой дизайна, а i18n
 * в объём гейта не входит (B29). Пустая строка здесь не годится: `Avatar`
 * считает инициалы из `name` (`shared/ui/Avatar.tsx:21`), и пустое имя дало бы
 * пустую подпись и пустую заглушку аватара.
 */
const NAME_FALLBACK = "Group"

/** Заголовки превью для типов без текста. */
const TYPE_PREVIEW: Record<string, string> = {
  image: "Photo",
  file: "File",
  voice: "Voice message",
}

const GENERIC_PREVIEW = "Message"

const DELETED_PREVIEW = "Message deleted"

/**
 * Превью последнего сообщения.
 *
 * Пустая строка здесь — не заглушка и не пробел в таблице: компонент уже
 * понимает её как «сообщений нет» (`ConversationListItem.tsx:26`) и рисует своё
 * «No messages yet». Адаптер кормит существующую ветку, а не заводит второй
 * текст для того же состояния.
 */
function previewOf(message: MessageDto | undefined): string {
  if (!message) return ""

  // Надгробие сильнее содержимого: у удалённого сообщения `payload` может
  // уцелеть, и показать его — значит показать то, что человек удалил.
  if (message.deletedAt != null) return DELETED_PREVIEW

  if (message.type === "text") {
    const text = message.payload?.text
    return typeof text === "string" && text !== "" ? text : GENERIC_PREVIEW
  }

  // `system` и всё, чего мы не знаем: `Message` честнее выдуманного заголовка.
  return TYPE_PREVIEW[message.type] ?? GENERIC_PREVIEW
}

function namesOf(participants: UserSummary[]): string {
  return participants
    .map((participant) => participant.displayName.trim())
    .filter((name) => name !== "")
    .join(", ")
}

/**
 * `now` — обязательный параметр, а не `new Date()` внутри.
 *
 * Форматтер времени принимает `now` явно, и скрывать его здесь значило бы
 * вернуть недетерминированность через чёрный ход: тесты адаптера проверяли бы
 * `14:22`/`Yesterday` относительно момента прогона. Обязательный параметр
 * заставляет каждый вызов назвать свои часы.
 *
 * `timestampOptions` — локаль и зона для отображения времени; по умолчанию
 * берутся у браузера, как и должно быть в интерфейсе. Параметр не для
 * производства, а для проверки: без него вывод адаптера зависел бы от зоны
 * машины, на которой идёт прогон, и «14:22» превращалось бы в «16:22» в
 * Берлине и «22:22» в Токио. Оба отступления от буквы раздела E.5 (там
 * сигнатура `(dto, currentUserId)`) названы в отчёте среза.
 */
export function adaptConversation(
  dto: ConversationDto,
  currentUserId: string,
  now: Date,
  timestampOptions: TimestampFormatOptions = {},
): Conversation {
  const others = dto.participants.filter((participant) => participant.userId !== currentUserId)
  const counterpart = dto.type === "direct" ? others[0] : undefined
  const lastMessage = dto.lastMessage

  return {
    id: dto.conversationId,
    name: namesOf(others) || NAME_FALLBACK,

    // Ключа нет — значит «неизвестно», и оно остаётся неизвестным. Ноль здесь
    // был бы уверенным ответом «всё прочитано», которого сервер не давал.
    unreadCount: dto.unreadCount,

    // Присутствие адаптер не выставляет **никогда**: сервер не отдаёт онлайн.
    // `lastSeenAt` — отметка из Postgres без признака «онлайн», и в G3-005 её
    // не показывает даже интерфейс (B17); поле живёт для G3-007.
    // Группе отметка не положена: одно значение времени на нескольких человек
    // семантически бессмысленно, и поданное как факт оно было бы выдумкой.
    lastSeenAt: counterpart?.lastSeenAt ?? undefined,

    lastMessagePreview: previewOf(lastMessage),
    previewDeleted: Boolean(lastMessage?.deletedAt),

    // Времени нет ровно тогда, когда нет сообщения: у беседы без `last_message`
    // выдуманного «только что» не бывает.
    lastMessageTimestamp: lastMessage
      ? formatConversationTimestamp(lastMessage.createdAt, now, timestampOptions)
      : undefined,
  }
}
