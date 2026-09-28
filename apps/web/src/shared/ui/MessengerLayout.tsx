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
}

export function MessengerLayout({ sidebar, children, banner }: MessengerLayoutProps) {
  return (
    <div className="flex h-dvh w-full flex-col overflow-hidden bg-surface text-on-surface">
      {banner}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {sidebar}
        <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-surface">{children}</main>
      </div>
    </div>
  )
}
