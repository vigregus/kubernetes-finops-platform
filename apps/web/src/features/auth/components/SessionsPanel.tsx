import type { DeviceSession } from "../../../shared/lib/types"
import { SessionRow } from "./SessionRow"

interface SessionsPanelProps {
  sessions: DeviceSession[]
  onLogOut: (id: string) => void
  onLogOutEverywhere: () => void
}

export function SessionsPanel({ sessions, onLogOut, onLogOutEverywhere }: SessionsPanelProps) {
  return (
    <div className="mx-auto flex w-full max-w-xl flex-col gap-4 p-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-on-surface">Active sessions</h2>
        <button type="button" onClick={onLogOutEverywhere} className="text-sm font-semibold text-status-error hover:underline">
          Log out everywhere
        </button>
      </div>
      <div className="flex flex-col gap-2">
        {sessions.map((session) => (
          <SessionRow key={session.id} session={session} onLogOut={onLogOut} />
        ))}
      </div>
    </div>
  )
}
