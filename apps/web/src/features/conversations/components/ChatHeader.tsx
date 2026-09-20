import type { Conversation } from "../../../shared/lib/types"
import { Avatar } from "../../../shared/ui/Avatar"
import { IconButton } from "../../../shared/ui/IconButton"

interface ChatHeaderProps {
  conversation: Conversation
}

function subtitle(conversation: Conversation) {
  if (conversation.typingNames?.length) {
    return { text: conversation.typingNames.length === 1 ? `${conversation.typingNames[0]} is typing…` : "Several people are typing…", dotClass: "bg-accent-terracotta animate-pulse" }
  }
  if (conversation.presence === "online") return { text: "Online", dotClass: "bg-status-success" }
  if (conversation.presence === "away") return { text: "Away", dotClass: "bg-status-warning" }
  if (conversation.lastSeenAt) return { text: `Last seen ${conversation.lastSeenAt}`, dotClass: "bg-text-warm-muted" }
  return { text: "Offline", dotClass: "bg-text-warm-muted" }
}

export function ChatHeader({ conversation }: ChatHeaderProps) {
  const { text, dotClass } = subtitle(conversation)
  const blocked = conversation.blockedByMe || conversation.blockedMe

  return (
    <header className="z-20 flex h-20 flex-shrink-0 items-center justify-between bg-surface/90 px-6 shadow-[0_1px_8px_rgba(41,37,36,0.04)] backdrop-blur-md">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <Avatar name={conversation.name} src={conversation.avatarUrl} presence={blocked ? undefined : conversation.presence} />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight text-on-surface">{conversation.name}</h1>
          <div className="flex items-center gap-1.5">
            {!blocked && <span className={`inline-block h-2 w-2 flex-shrink-0 rounded-full ${dotClass}`} />}
            <p className="truncate text-xs font-medium text-text-warm-secondary">{blocked ? "Unavailable" : text}</p>
          </div>
        </div>
      </div>

      <div className="flex flex-shrink-0 items-center gap-1">
        <IconButton icon="search" label="Search messages" />
        {!blocked && (
          <>
            <IconButton icon="call" label="Start voice call" />
            <IconButton icon="videocam" label="Start video call" />
          </>
        )}
        <div className="mx-1 h-6 w-px bg-surface-container-high" />
        <IconButton icon="info" label="Chat details" />
      </div>
    </header>
  )
}
