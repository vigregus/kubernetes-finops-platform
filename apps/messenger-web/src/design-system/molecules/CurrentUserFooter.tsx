import type { CurrentUser } from "../../types"
import { Avatar } from "../atoms/Avatar"
import { IconButton } from "../atoms/IconButton"

interface CurrentUserFooterProps {
  user: CurrentUser
  onOpenSettings?: () => void
}

export function CurrentUserFooter({ user, onOpenSettings }: CurrentUserFooterProps) {
  return (
    <div className="flex items-center justify-between rounded-xl bg-surface-cream p-2.5 shadow-[0_1px_4px_rgba(41,37,36,0.04)]">
      <div className="flex min-w-0 items-center gap-3">
        <Avatar name={user.name} src={user.avatarUrl} presence={user.presence} ringClassName="ring-surface-cream" />
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold leading-snug text-on-surface">{user.name}</p>
          <p className="truncate text-xs font-medium text-status-success">Active now</p>
        </div>
      </div>
      <IconButton icon="settings" label="Settings" onClick={onOpenSettings} />
    </div>
  )
}
