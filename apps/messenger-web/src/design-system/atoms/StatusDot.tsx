import { clsx } from "clsx"
import type { PresenceStatus } from "../../types"

const presenceColor: Record<PresenceStatus, string> = {
  online: "bg-status-success",
  away: "bg-status-warning",
  offline: "bg-text-warm-muted",
}

interface StatusDotProps {
  presence: PresenceStatus
  size?: "sm" | "md"
  ringClassName?: string
  className?: string
}

export function StatusDot({ presence, size = "md", ringClassName = "ring-surface", className }: StatusDotProps) {
  return (
    <span
      className={clsx(
        "absolute bottom-0 right-0 rounded-full ring-2",
        size === "sm" ? "h-2.5 w-2.5" : "h-3 w-3",
        presenceColor[presence],
        ringClassName,
        className,
      )}
      title={presence}
    />
  )
}
