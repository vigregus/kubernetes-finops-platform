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
  lastMessageTimestamp: string
  unreadCount?: number
  previewDeleted?: boolean
  /** names currently sending typing:start heartbeats (RT-001..004) */
  typingNames?: string[]
  /** 08-authorization.md — symmetric block: no write, no presence/typing, history stays readable */
  blockedByMe?: boolean
  blockedMe?: boolean
}

/**
 * 03-v1-scope.md "Состояния сообщения":
 * sending (optimistic, client-only) -> sent (committed) -> delivered (Kafka receipt) -> read (last_read_seq)
 * failed/retrying on error, reusing the same client_message_id.
 * "pending" = queued in the offline outbox (13-client-compatibility.md Часть 7), distinct from in-flight "sending".
 */
export type MessageDeliveryState = "sending" | "sent" | "delivered" | "read" | "pending" | "failed"

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

export interface CurrentUser {
  name: string
  handle: string
  avatarUrl?: string
  presence: PresenceStatus
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
