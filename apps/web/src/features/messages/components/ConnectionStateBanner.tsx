import { clsx } from "clsx"
import type { ConnectionState } from "../../../shared/lib/types"
import { Icon } from "../../../shared/ui/Icon"

interface ConnectionStateBannerProps {
  state: ConnectionState
}

/**
 * 03-v1-scope.md "Веб-клиент: состояния соединения" — recovered=true/false alone isn't
 * enough, the client must distinguish these 5 states and never show stale-as-current
 * while syncing.
 */
const copy: Record<Exclude<ConnectionState, "connected">, { icon: string; text: string; tone: string; spin?: boolean }> = {
  connecting: { icon: "sync", text: "Connecting…", tone: "text-status-warning", spin: true },
  disconnected: { icon: "cloud_off", text: "No connection. We'll reconnect automatically.", tone: "text-status-error" },
  degraded: { icon: "warning", text: "Limited connection — typing and presence are paused, messages still send.", tone: "text-status-warning" },
  syncing: { icon: "history", text: "Catching up on messages you may have missed…", tone: "text-status-warning", spin: true },
}

export function ConnectionStateBanner({ state }: ConnectionStateBannerProps) {
  if (state === "connected") return null
  const { icon, text, tone, spin } = copy[state]

  return (
    <div className="flex flex-shrink-0 justify-center px-6 pb-1 pt-3">
      <div className="inline-flex items-center gap-2 rounded-full bg-surface-cream px-3.5 py-1.5 text-xs shadow-[0_2px_6px_rgba(41,37,36,0.05)]">
        <Icon name={icon} size={16} className={clsx(tone, spin && "animate-spin")} />
        <span className="font-medium text-text-charcoal">{text}</span>
      </div>
    </div>
  )
}
