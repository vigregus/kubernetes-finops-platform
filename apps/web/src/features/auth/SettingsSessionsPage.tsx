import { useState } from "react"
import { SessionsPanel } from "./components/SessionsPanel"
import { deviceSessions } from "../../shared/lib/mock-data"

export function SettingsSessionsPage() {
  const [sessions, setSessions] = useState(deviceSessions)

  return (
    <div className="min-h-dvh bg-surface">
      <SessionsPanel
        sessions={sessions}
        onLogOut={(id) => setSessions((prev) => prev.filter((s) => s.id !== id))}
        onLogOutEverywhere={() => setSessions((prev) => prev.filter((s) => s.current))}
      />
    </div>
  )
}
