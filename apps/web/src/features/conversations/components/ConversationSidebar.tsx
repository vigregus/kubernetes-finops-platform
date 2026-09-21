import type { Conversation, CurrentUser } from "../../../shared/lib/types"
import { Icon } from "../../../shared/ui/Icon"
import { ConversationListItem } from "./ConversationListItem"
import { CurrentUserFooter } from "../../auth/components/CurrentUserFooter"

interface ConversationSidebarProps {
  conversations: Conversation[]
  /**
   * `null` — беседа не выбрана, и это настоящее состояние: `GET /conversations`
   * может вернуть ноль бесед, и подставлять тогда `conversations[0]` нечего.
   */
  activeConversationId: string | null
  currentUser: CurrentUser
  onSelectConversation: (id: string) => void
  /** Не передан — кнопки новой беседы нет: G3-005 беседы не создаёт. */
  onNewConversation?: () => void
}

export function ConversationSidebar({
  conversations,
  activeConversationId,
  currentUser,
  onSelectConversation,
  onNewConversation,
}: ConversationSidebarProps) {
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
          {onNewConversation && (
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
          )}
        </div>

        {/*
          `SearchField` здесь не рендерится, и это утверждение, а не пропуск.
          Поле — работающий локальный фильтр по **загруженному** массиву, а
          `GET /conversations` пагинирован, и пагинацию G3-005 намеренно не
          реализует: поле фильтровало бы первую страницу, обещая поиск по
          беседам вообще. Компонент и его stories остаются проектным запасом —
          поле вернётся тем срезом, который принесёт пагинацию.
        */}

        <div className="flex items-center justify-between px-6 py-1">
          <span className="text-xs font-semibold uppercase tracking-wider text-text-warm-muted">Chats</span>
          {/*
            Число подписано как число **загруженных**: список пагинирован, и
            голое `2` читалось бы как «столько бесед всего». Слово Active отсюда
            убрано отдельно и по той же причине, что «Active now» в подвале и
            «Offline» в шапке: оно читалось как «в сети», хотя считает беседы.
          */}
          <span className="rounded-full bg-surface-warm-subtle px-2 py-0.5 text-xs text-text-warm-secondary">
            {conversations.length} loaded
          </span>
        </div>

        <nav className="mt-1 flex-1 space-y-1 overflow-y-auto px-2">
          {conversations.map((conversation) => (
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
