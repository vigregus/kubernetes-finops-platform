import { Icon } from "../atoms/Icon"

interface BlockedNoticeProps {
  /** true when the other person blocked the current user (history stays readable, writing disabled) */
  blockedMe: boolean
  onUnblock?: () => void
}

/** 08-authorization.md — symmetric block: neither side can write; unblocking is explicit. */
export function BlockedNotice({ blockedMe, onUnblock }: BlockedNoticeProps) {
  return (
    <div className="flex flex-shrink-0 items-center justify-center gap-3 bg-surface-warm-subtle px-4 py-3 text-sm text-text-warm-secondary">
      <Icon name="block" size={18} className="text-status-error" />
      <span>{blockedMe ? "You can no longer message this person." : "You blocked this person."}</span>
      {!blockedMe && onUnblock && (
        <button type="button" onClick={onUnblock} className="font-semibold text-accent-terracotta hover:underline">
          Unblock
        </button>
      )}
    </div>
  )
}
