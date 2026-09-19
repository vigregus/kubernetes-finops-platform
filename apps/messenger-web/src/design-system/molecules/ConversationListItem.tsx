import { clsx } from "clsx"
import type { Conversation } from "../../types"
import { Avatar } from "../atoms/Avatar"
import { Badge } from "../atoms/Badge"
import { Icon } from "../atoms/Icon"

interface ConversationListItemProps {
  conversation: Conversation
  active?: boolean
  onSelect: (id: string) => void
}

export function ConversationListItem({ conversation, active, onSelect }: ConversationListItemProps) {
  const {
    id,
    name,
    avatarUrl,
    initials,
    presence,
    lastMessagePreview,
    lastMessageTimestamp,
    unreadCount,
    previewDeleted,
    typingNames,
  } = conversation
  const isEmpty = !lastMessagePreview && !previewDeleted

  return (
    <button
      type="button"
      onClick={() => onSelect(id)}
      className={clsx(
        "group relative flex w-full items-center gap-3 rounded-xl p-3 text-left transition-all",
        active
          ? "bg-surface-cream text-on-surface shadow-[0_2px_8px_rgba(41,37,36,0.06)]"
          : "text-on-surface-variant hover:bg-surface-cream/70 hover:text-on-surface",
      )}
    >
      {active && <span className="absolute bottom-2 left-0 top-2 w-1.5 rounded-r-full bg-accent-terracotta" />}
      <Avatar name={name} src={avatarUrl} initials={initials} size="md" presence={presence} ringClassName={active ? "ring-surface-cream" : "ring-surface-container-low"} />
      <div className="min-w-0 flex-1">
        <div className="mb-0.5 flex items-center justify-between">
          <span className={clsx("truncate text-sm", active ? "font-semibold" : "font-medium")}>{name}</span>
          <span className={clsx("text-xs", active ? "font-medium text-accent-terracotta" : "text-text-warm-muted")}>
            {lastMessageTimestamp}
          </span>
        </div>
        {typingNames?.length ? (
          <p className="truncate text-sm font-medium text-accent-terracotta">
            {typingNames.length === 1 ? `${typingNames[0]} is typing…` : "Typing…"}
          </p>
        ) : previewDeleted ? (
          <div className="flex items-center gap-1.5 text-text-warm-muted">
            <Icon name="block" size={15} className="opacity-70" />
            <p className="truncate text-sm italic">{lastMessagePreview}</p>
          </div>
        ) : isEmpty ? (
          <p className="truncate text-sm italic text-text-warm-muted">No messages yet</p>
        ) : (
          <div className="flex items-center justify-between gap-2">
            <p
              className={clsx(
                "truncate text-sm",
                unreadCount ? "font-medium text-on-surface" : "text-text-warm-secondary",
              )}
            >
              {lastMessagePreview}
            </p>
            {Boolean(unreadCount) && <Badge count={unreadCount!} />}
          </div>
        )}
      </div>
    </button>
  )
}
