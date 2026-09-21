// Stand-in for the client generated from packages/contracts/openapi.yaml (07-engineering-standard.md
// Часть 6: "Типы не пишутся руками"). Delete once `api/generated/` is wired — G3-005.
// State shapes mirror docs/messenger/{01-architecture,02-delivery,03-v1-scope,08-authorization,13-client-compatibility}.md

export type PresenceStatus = "online" | "away" | "offline"

/** 03-v1-scope.md "Веб-клиент: состояния соединения" — 5 states, recovered flag alone is not enough. */
export type ConnectionState = "connected" | "connecting" | "disconnected" | "degraded" | "syncing"

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
  previewDeleted?: boolean
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
