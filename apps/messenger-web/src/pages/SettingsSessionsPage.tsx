import { useState } from "react"
import { SessionsPanel } from "../design-system/organisms/SessionsPanel"
import { deviceSessions } from "../data/mockData"

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
