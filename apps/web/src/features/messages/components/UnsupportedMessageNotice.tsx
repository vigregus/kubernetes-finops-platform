import { clsx } from "clsx"
import { Icon } from "../../../shared/ui/Icon"

interface UnsupportedMessageNoticeProps {
  isOwn?: boolean
  timestamp: string
}

/**
 * CLI-001: a message type from a newer client build must still occupy its seq slot
 * and count toward unread — never silently skipped.
 */
export function UnsupportedMessageNotice({ isOwn, timestamp }: UnsupportedMessageNoticeProps) {
  return (
    <div className={clsx("flex", isOwn ? "justify-end" : "justify-start")}>
      <div className="flex max-w-[70%] items-center gap-2 rounded-2xl border border-dashed border-outline-variant bg-surface-container-low px-3.5 py-2.5 text-sm text-text-warm-secondary">
        <Icon name="help_outline" size={16} className="flex-shrink-0 opacity-70" />
        <span className="italic">This message needs a newer version of the app to display.</span>
        <span className="ml-1 flex-shrink-0 text-xs not-italic text-text-warm-muted">{timestamp}</span>
      </div>
    </div>
  )
}
