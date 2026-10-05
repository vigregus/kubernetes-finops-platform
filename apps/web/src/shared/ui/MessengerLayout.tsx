import type { ReactNode } from "react"

interface MessengerLayoutProps {
  sidebar: ReactNode
  children: ReactNode
  /**
   * Full-width strip above sidebar and main, e.g. `EmailVerificationBanner`
   * (`G3-007-1a`). Optional and absent by default rather than an empty slot
   * always rendered: an empty banner row would still claim a `flex-shrink-0`
   * line's worth of height even at zero content.
   */
  banner?: ReactNode
  /**
   * Что показано на узком экране: список бесед или сама беседа — по очереди, а не
   * рядом. `both` — широкий экран (и умолчание): обе колонки, как раньше.
   */
  screen?: "both" | "list" | "chat"
}

export function MessengerLayout({ sidebar, children, banner, screen = "both" }: MessengerLayoutProps) {
  return (
    <div
      data-screen={screen}
      className="p-safe flex h-dvh w-full flex-col overflow-hidden bg-surface text-on-surface"
    >
      {banner}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Не скрывается стилем, а не рисуется: у скрытой панели беседы живо соединение. */}
        {screen !== "chat" && sidebar}
        {screen !== "list" && (
          <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-surface">{children}</main>
        )}
      </div>
    </div>
  )
}
