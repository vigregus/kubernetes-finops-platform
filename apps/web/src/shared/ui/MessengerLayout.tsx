import type { ReactNode } from "react"

interface MessengerLayoutProps {
  sidebar: ReactNode
  children: ReactNode
}

export function MessengerLayout({ sidebar, children }: MessengerLayoutProps) {
  return (
    <div className="flex h-dvh w-full overflow-hidden bg-surface text-on-surface">
      {sidebar}
      <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-surface">{children}</main>
    </div>
  )
}
