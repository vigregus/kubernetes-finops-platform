import { useState } from "react"
import type { Conversation, CurrentUser } from "../../../shared/lib/types"
import { Icon } from "../../../shared/ui/Icon"
import { ConversationListItem } from "./ConversationListItem"
import { CurrentUserFooter } from "../../auth/components/CurrentUserFooter"
import { SearchField } from "../../../shared/ui/SearchField"

interface ConversationSidebarProps {
  conversations: Conversation[]
  activeConversationId: string
  currentUser: CurrentUser
  onSelectConversation: (id: string) => void
  onNewConversation?: () => void
}

export function ConversationSidebar({
  conversations,
  activeConversationId,
  currentUser,
  onSelectConversation,
  onNewConversation,
}: ConversationSidebarProps) {
  const [query, setQuery] = useState("")
  const visible = conversations.filter((c) => c.name.toLowerCase().includes(query.toLowerCase()))

  return (
    <aside className="flex w-[360px] flex-shrink-0 flex-col justify-between bg-surface-container-low shadow-[2px_0_16px_rgba(41,37,36,0.03)]">
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex h-20 items-center justify-between px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-terracotta text-on-primary shadow-sm">
              <Icon name="forum" size={22} />
            </div>
            <div>
              <span className="text-lg font-semibold tracking-tight text-on-surface">Vector</span>
              <p className="text-xs text-text-warm-muted">Messages &amp; People</p>
            </div>
          </div>
          <button
            type="button"
            aria-label="New conversation"
            title="Start new chat"
            onClick={onNewConversation}
            className="group flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-surface-cream text-on-surface shadow-[0_2px_6px_rgba(41,37,36,0.04)] transition-all hover:bg-surface-warm-subtle active:scale-95"
          >
            <Icon
              name="edit_square"
              size={20}
              className="text-accent-terracotta transition-transform duration-200 group-hover:rotate-45"
            />
          </button>
        </div>

        <div className="px-6 pb-3">
          <SearchField placeholder="Search chats..." value={query} onChange={setQuery} />
        </div>

        <div className="flex items-center justify-between px-6 py-1">
          <span className="text-xs font-semibold uppercase tracking-wider text-text-warm-muted">Chats</span>
          <span className="rounded-full bg-surface-warm-subtle px-2 py-0.5 text-xs text-text-warm-secondary">
            {conversations.length} Active
          </span>
        </div>

        <nav className="mt-1 flex-1 space-y-1 overflow-y-auto px-2">
          {visible.map((conversation) => (
            <ConversationListItem
              key={conversation.id}
              conversation={conversation}
              active={conversation.id === activeConversationId}
              onSelect={onSelectConversation}
            />
          ))}
        </nav>
      </div>

      <div className="bg-surface-container-low/95 p-3">
        <CurrentUserFooter user={currentUser} />
      </div>
    </aside>
  )
}
