import type { DeviceSession } from "../../types"
import { Icon } from "../atoms/Icon"

interface SessionRowProps {
  session: DeviceSession
  onLogOut: (id: string) => void
}

/** 08-authorization.md Часть 3 "Сессия" — AUTH-002/003: log out this device vs. everywhere. */
export function SessionRow({ session, onLogOut }: SessionRowProps) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl bg-surface-cream p-3 shadow-[0_1px_4px_rgba(41,37,36,0.04)]">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-surface-container-high text-text-warm-secondary">
          <Icon name="devices" size={20} />
        </div>
        <div>
          <p className="flex items-center gap-2 text-sm font-medium text-on-surface">
            {session.deviceLabel}
            {session.current && (
              <span className="rounded-full bg-status-success/15 px-2 py-0.5 text-[11px] font-semibold text-status-success">
                This device
              </span>
            )}
          </p>
          <p className="text-xs text-text-warm-muted">
            {session.userAgent} · last active {session.lastSeenAt}
          </p>
        </div>
      </div>
      {!session.current && (
        <button
          type="button"
          onClick={() => onLogOut(session.id)}
          className="text-xs font-semibold text-status-error hover:underline"
        >
          Log out
        </button>
      )}
    </div>
  )
}
