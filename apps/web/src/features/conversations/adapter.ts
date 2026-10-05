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
 * `unreadCount` при отсутствии ключа («неизвестно» — не то же самое, что «всё
 * прочитано»), необязательное `lastMessageTimestamp`, пустое превью у беседы
 * без сообщений и `presence`, которого нет при отсутствии ключа `online`:
 * отсутствие ключа — это «сервер не сказал», а не «офлайн».
 */

import { callPreview, parseCallSummary } from "../calls/callSummary"
import type {
  Conversation as ConversationDto,
  ConversationListPage,
  ConversationReadStatesInner,
  Message as MessageDto,
  UserSummary,
} from "../../api/generated"
import type { Conversation, PeerReadState, PresenceStatus } from "../../shared/lib/types"
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
function previewOf(message: MessageDto | undefined, viewerId: string): string {
  if (!message) return ""

  // Надгробие сильнее содержимого: у удалённого сообщения `payload` может
  // уцелеть, и показать его — значит показать то, что человек удалил.
  if (message.deletedAt != null) return DELETED_PREVIEW

  if (message.type === "text") {
    const text = message.payload?.text
    return typeof text === "string" && text !== "" ? text : GENERIC_PREVIEW
  }

  // Итог звонка — единственное служебное сообщение, у которого есть свои слова.
  if (message.type === "system") {
    const call = parseCallSummary(message.payload?.text)
    if (call !== null) return callPreview(call, message.senderId === viewerId)
  }

  // Остальное `system` и всё, чего мы не знаем: `Message` честнее выдуманного заголовка.
  return TYPE_PREVIEW[message.type] ?? GENERIC_PREVIEW
}

function namesOf(participants: UserSummary[]): string {
  return participants
    .map((participant) => participant.displayName.trim())
    .filter((name) => name !== "")
    .join(", ")
}

/**
 * `online` → слово модели: **три** исхода, а не два.
 *
 * Отсутствие ключа остаётся отсутствием. Свёртка в два исхода
 * (`online ? "online" : "away"`) сказала бы «Away» на ответе, где сервер о
 * присутствии молчал вовсе, — а это ровно тот класс, против которого написан
 * докстринг `ChatHeader.tsx`: утверждение о человеке, которого сервер не делал.
 * Два разных «нет» — «активности не было» (`false`) и «сервер не сказал» (ключа
 * нет) — обязаны доехать до разметки разными: первое словом «Offline», второе
 * отсутствием подзаголовка.
 *
 * Слово для `false` — `offline`, а не `away`. `away` в семантике мессенджера
 * значит «в сети, но отошёл», а сервер такого не знает вовсе: он знает ровно
 * одно — подтверждённой активности не было объявленное окно (`domain/presence.py`,
 * 180 секунд). Сказать больше сервера здесь — то же самое, что сказать что-то
 * на его молчании, только другими словами.
 *
 * `offline` в словаре модели уже есть (`shared/lib/types.ts:5`), и `StatusDot`
 * рисует его тем же цветом, что и здесь: одно слово — один цвет, где бы оно ни
 * появилось.
 */
function presenceOf(online: boolean | undefined): PresenceStatus | undefined {
  if (online === undefined) return undefined
  return online ? "online" : "offline"
}

/**
 * Состояние чтения собеседника в `read_states` — ищется **по имени**, а не по
 * месту в массиве.
 *
 * Массив приходит включая зрителя и в порядке входа в беседу, то есть зритель в
 * нём первый: `read_states[0]` показал бы человеку его собственное прочтение как
 * прочтение собеседника. Это ровно тот класс, что «имя из `participants[0]`», и
 * лечится он тем же — сравнением `user_id`.
 *
 * Массив **разреженный**: элемента нет у того, кто квитанции не прислал
 * (`NULL` в сборке сервера — не ноль). Отсутствие остаётся отсутствием:
 * дописать отсутствующему пару нулей значило бы объявить прочтение, которого он
 * не делал, и откатить уже показанную отметку — то же правило, что у
 * `unreadCount` и `lastSeenAt`.
 *
 * Чужое состояние здесь не маскируется и не может: за блокировку маскирует
 * **сервер** (её элемента в ответе просто нет), а клиент, достраивающий маску
 * сам, разошёлся бы с REST при первой же правке правила.
 *
 * Группе состояния нет по той же причине, что и присутствия: одно число на
 * нескольких человек семантически бессмысленно.
 */
function peerReadStateOf(
  states: ConversationReadStatesInner[] | undefined,
  counterpart: UserSummary | undefined,
): PeerReadState | undefined {
  if (counterpart === undefined || states === undefined) return undefined

  const state = states.find((entry) => entry.userId === counterpart.userId)
  if (state === undefined) return undefined

  // Колоночные имена → имена модели. Перевод в одной точке, как у `_user_summary`
  // на сервере: иначе `last_read_seq` и `readSeq` разъехались бы по файлам.
  return { readSeq: state.lastReadSeq, deliveredSeq: state.lastDeliveredSeq }
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

    // Присутствие — из `online`, и словом: см. `presenceOf`. Группе оно не
    // положено по той же причине, что и отметка: одно значение на нескольких
    // человек семантически бессмысленно, и поданное как факт оно было бы
    // выдумкой.
    presence: counterpart ? presenceOf(counterpart.online) : undefined,

    // `lastSeenAt` — отметка из Postgres, и живёт она **рядом** с присутствием,
    // а не вместо него: отметка не выводится из признака и признак не выводится
    // из отметки. Отметка переживает потерю realtime-слоя, присутствие —
    // предикат по времени ответа; это два разных ответа о человеке.
    lastSeenAt: counterpart?.lastSeenAt ?? undefined,

    // Кто здесь собеседник — по имени, а не по месту: квитанция события несёт
    // `reader_id`, и без этого имени панель не отличила бы его квитанцию от
    // собственной, пришедшей на тот же канал.
    peerUserId: counterpart?.userId,

    // Номера собеседника — рядом с присутствием и по той же причине: это факт о
    // **нём**, приходящий из того же ответа, и живут они независимо (квитанция
    // переживает потерю realtime-слоя так же, как отметка времени).
    peerReadState: peerReadStateOf(dto.readStates, counterpart),

    lastMessagePreview: previewOf(lastMessage, currentUserId),
    previewDeleted: Boolean(lastMessage?.deletedAt),

    // Отдельный признак, а не «превью непустое»: от него зависит, можно ли
    // говорить «No messages yet», и держать это на договорённости таблицы
    // превью значило бы выводить утверждение о сообщениях из их текста.
    hasMessages: lastMessage !== undefined,

    // Времени нет ровно тогда, когда нет сообщения: у беседы без `last_message`
    // выдуманного «только что» не бывает.
    lastMessageTimestamp: lastMessage
      ? formatConversationTimestamp(lastMessage.createdAt, now, timestampOptions)
      : undefined,
  }
}

/**
 * Страница `GET /conversations` → модели списка.
 *
 * Отдельная функция, а не `page.items.map(...)` на месте вызова: зритель у
 * страницы **один**, и он обязан быть тем же для всех её бесед — иначе одна
 * беседа показала бы собеседника, а соседняя в той же строке — самого
 * зрителя. Здесь же названо и правило порядка: адаптация идёт **после** `/me`,
 * потому что до него `currentUserId` взять неоткуда.
 *
 * `page.nextBefore*` не читаются: пагинация в объём G3-005 не входит, и
 * притвориться, что страница — это весь список, интерфейс не должен (B28).
 */
export function adaptConversations(
  page: ConversationListPage,
  currentUserId: string,
  now: Date,
  timestampOptions: TimestampFormatOptions = {},
): Conversation[] {
  return page.items.map((dto) => adaptConversation(dto, currentUserId, now, timestampOptions))
}

/**
 * Своё сообщение в строке списка — **своим** фактом, а не ожиданием события.
 *
 * Список обязан показать отправленное, не дожидаясь `message.created` (D13), и
 * не потому, что событие медленное, а потому, что оно **необязательное**:
 * публикация best-effort (`G3-007` D11), и при потерянной публикации строка
 * осталась бы со старым превью до следующей сверки — то есть человек видел бы
 * «ничего не отправлено» там, где отправлено. Отсюда правило: событие ускоряет,
 * отправка решает.
 *
 * Функция **чистая**, и это не украшение: её предмет — три поля строки и её
 * место в списке, а не транспорт. Поэтому же она не трогает `unreadCount`:
 * своё сообщение непрочитанного не прибавляет никогда, и прибавление здесь было
 * бы утверждением «вы написали себе».
 *
 * `pending` — состояние **той же** строки, а не отдельной метки: пока сервер не
 * ответил, превью помечено неподтверждённым, а после ответа пометка снимается
 * той же функцией. Два разных поля разошлись бы: снятие пришлось бы помнить
 * отдельно от простановки.
 *
 * Беседы, которой в списке нет, касаться нечего: `POST /conversations` кладёт
 * новую беседу в список **отдельно** (`ChatPage`), и выдумывать здесь строку
 * значило бы завести второе место, собирающее модель из ответа создания.
 */
export interface SentPreview {
  readonly conversationId: string
  readonly text: string
  /** Момент, которым помечается строка: подтверждение — своё, нажатие — своё. */
  readonly at: Date
  /** Сервер ещё не подтвердил: сообщение держит очередь. */
  readonly pending: boolean
}

export function withSentPreview(
  conversations: readonly Conversation[],
  sent: SentPreview,
  timestampOptions: TimestampFormatOptions = {},
): Conversation[] {
  const index = conversations.findIndex((conversation) => conversation.id === sent.conversationId)
  // Беседы нет — менять нечего. Не «добавим на всякий случай»: строка списка
  // для беседы, которой сервер не называл, утверждала бы о ней больше, чем
  // известно, а у `withSentPreview` нет ни имени собеседника, ни участников.
  if (index === -1) return conversations as Conversation[]

  const current = conversations[index]!
  // Пометка снимается **отсутствием** поля, а не значением `false`: так у
  // непомеченной строки превью выглядит ровно как у всякой другой, и третьего
  // состояния («поле есть, но ложно») в модели не заводится.
  const { pendingPreview: _confirmed, ...rest } = current

  const updated: Conversation = {
    ...rest,
    lastMessagePreview: sent.text,
    // Время — своё, а не из ответа: ответ несёт `created_at` сервера, но строка
    // обновляется и до него, и брать часы из объекта, которого ещё нет, нечем.
    lastMessageTimestamp: formatConversationTimestamp(
      sent.at.toISOString(),
      sent.at,
      timestampOptions,
    ),
    // Сообщение существует с момента нажатия — и это утверждение человека, а не
    // сервера: строка про «нет сообщений» была бы неправдой уже тогда.
    hasMessages: true,
    // Своё сообщение удалённым быть не может: надгробий в этом гейте нет.
    previewDeleted: false,
    ...(sent.pending ? { pendingPreview: true } : {}),
  }

  // Свежая беседа сверху, по времени последней активности (D13). Порядок
  // переставляется **здесь**, а не сортировкой всего списка: сортировка по
  // `lastMessageTimestamp` была бы сортировкой по display-строке (`14:22`,
  // `Yesterday`), то есть по тексту, а не по времени.
  return [updated, ...conversations.slice(0, index), ...conversations.slice(index + 1)]
}
