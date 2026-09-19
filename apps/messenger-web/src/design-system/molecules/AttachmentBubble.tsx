import { clsx } from "clsx"
import type { MessageAttachment } from "../../types"
import { Icon } from "../atoms/Icon"

interface AttachmentBubbleProps {
  attachment: MessageAttachment
  own?: boolean
}

const kindIcon = { image: "image", file: "description", voice: "graphic_eq" } as const

function formatDuration(seconds?: number) {
  if (!seconds) return "0:00"
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, "0")}`
}

export function AttachmentBubble({ attachment, own }: AttachmentBubbleProps) {
  const { kind, name, state, previewUrl, durationSeconds, sizeLabel, rejectionReason } = attachment

  const shell = clsx(
    "flex items-center gap-3 rounded-2xl p-3 text-sm",
    own ? "rounded-br-sm bg-accent-terracotta text-on-primary" : "rounded-bl-sm bg-surface-cream text-text-charcoal shadow-[0_1px_4px_rgba(41,37,36,0.05)]",
    state === "rejected" && "border border-status-error/40 bg-error-container/40 text-status-error",
  )

  if (kind === "image" && state === "ready") {
    return (
      <div className={clsx("overflow-hidden rounded-2xl", own ? "rounded-br-sm" : "rounded-bl-sm")}>
        <img src={previewUrl} alt={name} className="block max-h-72 w-64 object-cover" />
      </div>
    )
  }

  return (
    <div className={shell}>
      <div
        className={clsx(
          "flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl",
          own ? "bg-white/20" : "bg-surface-warm-subtle",
        )}
      >
        <Icon name={kindIcon[kind]} size={20} />
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{name}</p>
        <p className={clsx("text-xs", own ? "text-on-primary/80" : "text-text-warm-muted")}>
          {state === "uploading" && "Uploading…"}
          {state === "processing" && "Checking file…"}
          {state === "ready" && (kind === "voice" ? formatDuration(durationSeconds) : sizeLabel)}
          {state === "rejected" && (rejectionReason ?? "Couldn't send this file")}
          {state === "failed" && "Upload failed"}
        </p>
      </div>
      {(state === "uploading" || state === "processing") && (
        <Icon name="progress_activity" size={18} className="animate-spin flex-shrink-0 opacity-70" />
      )}
      {state === "rejected" && <Icon name="error_outline" size={18} className="flex-shrink-0 text-status-error" />}
      {state === "ready" && kind === "voice" && <Icon name="play_arrow" size={20} className="flex-shrink-0" />}
    </div>
  )
}
