// Stand-in for the client generated from packages/contracts/openapi.yaml (07-engineering-standard.md
// Часть 6: "Типы не пишутся руками"). Delete once `api/generated/` is wired — G3-005.
// State shapes mirror docs/messenger/{01-architecture,02-delivery,03-v1-scope,08-authorization,13-client-compatibility}.md

export type PresenceStatus = "online" | "away" | "offline"

/** 03-v1-scope.md "Веб-клиент: состояния соединения" — 5 states, recovered flag alone is not enough. */
export type ConnectionState = "connected" | "connecting" | "disconnected" | "degraded" | "syncing"

/**
 * Состояние чтения собеседника — два числа о **беседе**, а не о человеке.
 *
 * `readSeq` — докуда собеседник прочитал, `deliveredSeq` — докуда дошло до его
 * устройства. Вывести второе из первого нельзя: «доставлено, но не прочитано» —
 * это ровно их разность, и состояние сообщения (`MessageDeliveryState`) читается
 * по обоим. У номера прочтения нет смысла вне конкретной ленты, поэтому он живёт
 * на беседе, а не на `UserSummary`: положить его в участника значило бы
 * повторять ошибку контекста, от которой спасает само имя типа.
 */
export interface PeerReadState {
  readSeq: number
  deliveredSeq: number
}

export interface Conversation {
  id: string
  name: string
  avatarUrl?: string
  initials?: string
  presence?: PresenceStatus
  lastSeenAt?: string
  lastMessagePreview: string
  /**
   * Display-строка (`14:22`, `Yesterday`, `Sep 20`), а не ISO: компонент рисует
   * поле безусловно, и прокинутый `created_at` уехал бы в список как есть.
   *
   * Необязательно: у беседы без `last_message` честного времени нет, и
   * выдуманного «только что» не бывает. `undefined` в разметке рисуется
   * пустотой — ровно то, что нужно.
   */
  lastMessageTimestamp?: string
  /** Ключа нет — «неизвестно»; это не ноль и не «всё прочитано». */
  unreadCount?: number
  /**
   * Кто здесь собеседник — по этому имени панель узнаёт его квитанцию.
   *
   * Событие `message.read` несёт `reader_id`, а состояние чтения в REST приходит
   * массивом **без** адреса в беседе: ключ там `user_id`, и в личной беседе их
   * два. Без имени собеседника в модели вкладка не отличила бы его квитанцию от
   * собственной — а своя приходит на тот же канал.
   *
   * Пусто у группы: собеседник там не один, и «его» номер прочтения не
   * существует (то же правило, что у присутствия и отметки времени).
   */
  peerUserId?: string
  /**
   * Номера собеседника — его прочтение и его доставка.
   *
   * `undefined` — «сервер об этом не сообщал», и это не пара нулей: массив
   * `read_states` разреженный, элемента нет у того, кто квитанции не присылал, а
   * `{readSeq: 0}` был бы утверждением «прочитано ни до чего» и откатил бы уже
   * показанную отметку. Тот же `absence ≠ zero`, что у `unreadCount` и
   * `lastSeenAt`.
   */
  peerReadState?: PeerReadState
  previewDeleted?: boolean
  /**
   * Сервер сообщил последнее сообщение — `last_message` в ответе есть.
   *
   * Отдельный признак, а не «превью непустое»: от него зависит, можно ли
   * говорить «No messages yet». Выведенное из пустоты строки, это утверждение
   * держалось бы на договорённости таблицы превью, а не на факте.
   */
  hasMessages: boolean
  /** names currently sending typing:start heartbeats (RT-001..004) */
  typingNames?: string[]
  /** 08-authorization.md — symmetric block: no write, no presence/typing, history stays readable */
  blockedByMe?: boolean
  blockedMe?: boolean
}

/**
 * 07-engineering-standard.md Часть 6 — the client FSM, same names as v1 scope:
 * sending (optimistic, client-only) -> sent (committed) -> delivered (Kafka receipt) -> read (last_read_seq),
 * plus failed and retrying. "retrying" also covers a message queued in the offline outbox
 * (13-client-compatibility.md Часть 7) waiting for connectivity to attempt again — both cases
 * reuse the same client_message_id on the next attempt.
 */
export type MessageDeliveryState = "sending" | "sent" | "delivered" | "read" | "retrying" | "failed"

export type AttachmentKind = "image" | "file" | "voice"
/** 03-v1-scope.md "Вложения": uploading -> processing -> ready, or rejected/failed. */
export type AttachmentState = "uploading" | "processing" | "ready" | "rejected" | "failed"

export interface MessageAttachment {
  kind: AttachmentKind
  name: string
  state: AttachmentState
  url?: string
  /** for images */
  previewUrl?: string
  /** for voice, in seconds — shown before upload completes (ATT-004) */
  durationSeconds?: number
  sizeLabel?: string
  rejectionReason?: string
}

export type MessageKind = "text" | "attachment" | "unsupported"

export interface ChatMessage {
  id: string
  /**
   * Номер сообщения в беседе (`conversation_seq`) — **не** время и не индекс
   * в массиве. `07-engineering-standard.md:78` называет его «наибольшим
   * применённым `conversation_seq` беседы; он же признак повтора».
   *
   * Поле обязательно, потому что номер есть у каждого сообщения: он
   * проставляется сервером при коммите, и мок-данные запаса держат его наравне
   * с настоящими. Необязательное поле означало бы «у сообщения может не быть
   * номера», и порядок ленты стал бы недоказуем — а он и есть предмет G3-006:
   * по номеру распознаётся пропуск, по нему же лента доказывается монотонной.
   *
   * Уникален **внутри беседы**, не глобально: две беседы обе начинаются с
   * единицы, и склейка лент двух бесед по этому полю была бы ошибкой.
   */
  seq: number
  authorId: "me" | string
  authorName?: string
  avatarUrl?: string
  kind: MessageKind
  text?: string
  attachment?: MessageAttachment
  timestamp: string
  deliveryState?: MessageDeliveryState
  deleted?: boolean
  /** CLI-004: a queued offline send that the server rejected once replayed (e.g. sender got blocked) */
  failureReason?: string
}

/**
 * Текущий пользователь — то, что о нём действительно известно.
 *
 * `handle` и `presence` из типа **удалены**, а не оставлены незаполненными:
 * `GET /me` не отдаёт ни того, ни другого. Поле, которое никто не заполняет, —
 * это приглашение заполнить его догадкой: `@handle` вывели бы из адреса, а
 * `presence` — «на глазок», и интерфейс начал бы сообщать факты, которых сервер
 * не сообщал. Домен прямо разделяет два `last_seen_at` (`03-v1-scope.md:77-83`)
 * и отдаёт наружу отметку **без** признака «онлайн» (`README.md:78`).
 * Единственный потребитель этих полей — подвал, и он их не рисует.
 */
export interface CurrentUser {
  name: string
  avatarUrl?: string
  emailVerified?: boolean
  email?: string
}

/** 08-authorization.md Часть 3 "Сессия" — multi-device session list. */
export interface DeviceSession {
  id: string
  deviceLabel: string
  userAgent: string
  lastSeenAt: string
  current?: boolean
}
