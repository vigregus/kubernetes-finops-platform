import { clsx } from "clsx"
import type { ChatMessage } from "../../../shared/lib/types"
import { Avatar } from "../../../shared/ui/Avatar"
import { Icon } from "../../../shared/ui/Icon"
import { AttachmentBubble } from "./AttachmentBubble"
import { UnsupportedMessageNotice } from "./UnsupportedMessageNotice"

interface MessageBubbleProps {
  message: ChatMessage
  onRetry?: (id: string) => void
}

function DeliveryStatus({ message, onRetry }: { message: ChatMessage; onRetry?: (id: string) => void }) {
  switch (message.deliveryState) {
    case "read":
      return (
        <div className="mr-1 mt-1 flex items-center gap-1.5">
          <span className="text-xs text-text-warm-muted">{message.timestamp}</span>
          <Icon name="done_all" size={16} className="text-accent-terracotta" />
        </div>
      )
    case "delivered":
      return (
        <div className="mr-1 mt-1 flex items-center gap-1.5">
          <span className="text-xs text-text-warm-muted">{message.timestamp}</span>
          <Icon name="done_all" size={16} className="text-text-warm-muted" />
        </div>
      )
    case "sent":
      return (
        <div className="mr-1 mt-1 flex items-center gap-1.5">
          <span className="text-xs text-text-warm-muted">{message.timestamp}</span>
          <Icon name="done" size={16} className="text-text-warm-muted" />
        </div>
      )
    case "sending":
      return (
        <div className="mr-1 mt-1 flex items-center gap-1.5 text-text-warm-muted">
          <span className="text-xs">Sending…</span>
          <Icon name="progress_activity" size={14} className="animate-spin" />
        </div>
      )
    case "retrying":
      return (
        <div className="mr-1 mt-1 flex items-center gap-1.5 text-text-warm-muted">
          <span className="text-xs font-medium text-accent-amber">Retrying…</span>
          <Icon name="schedule" size={15} className="animate-pulse text-accent-amber" />
        </div>
      )
    case "failed":
      return (
        <div className="mr-1 mt-1 flex flex-col items-end gap-0.5">
          <div className="flex items-center gap-2">
            <span className="flex items-center gap-1 text-xs font-medium text-status-error">
              <Icon name="error_outline" size={14} />
              {message.failureReason ?? "Not sent"}
            </span>
            <button
              type="button"
              onClick={() => onRetry?.(message.id)}
              className="flex items-center gap-0.5 text-xs font-semibold text-accent-terracotta hover:underline"
            >
              <Icon name="refresh" size={14} />
              Retry
            </button>
          </div>
        </div>
      )
    default:
      return <span className="mr-1 mt-1 text-xs text-text-warm-muted">{message.timestamp}</span>
  }
}

export function MessageBubble({ message, onRetry }: MessageBubbleProps) {
  const isOwn = message.authorId === "me"
  const isFailed = message.deliveryState === "failed"

  if (message.kind === "unsupported") {
    return <UnsupportedMessageNotice isOwn={isOwn} timestamp={message.timestamp} />
  }

  const body =
    message.kind === "attachment" && message.attachment ? (
      <AttachmentBubble attachment={message.attachment} own={isOwn} />
    ) : (
      <div
        className={clsx(
          "rounded-2xl p-3.5 text-sm leading-relaxed",
          isOwn
            ? clsx(
                "rounded-br-sm text-on-primary",
                isFailed ? "bg-surface-container text-text-charcoal opacity-80" : "bg-accent-terracotta shadow-[0_2px_8px_rgba(234,88,12,0.2)]",
              )
            : "rounded-bl-sm bg-surface-cream text-text-charcoal shadow-[0_1px_4px_rgba(41,37,36,0.05)]",
        )}
      >
        {message.text}
      </div>
    )

  if (isOwn) {
    return (
      <div className="flex items-end justify-end gap-2">
        <div className="flex max-w-[70%] flex-col items-end">
          {body}
          <DeliveryStatus message={message} onRetry={onRetry} />
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-end gap-2.5">
      <Avatar name={message.authorName ?? "Contact"} src={message.avatarUrl} size="sm" />
      <div className="flex max-w-[70%] flex-col items-start">
        <span className="mb-1 ml-1 text-xs text-text-warm-muted">{message.authorName}</span>
        {body}
        <span className="ml-1 mt-1 text-xs text-text-warm-muted">{message.timestamp}</span>
      </div>
    </div>
  )
}
