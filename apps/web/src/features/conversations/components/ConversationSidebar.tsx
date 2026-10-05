import type { Conversation, CurrentUser } from "../../../shared/lib/types"
import { Avatar } from "../../../shared/ui/Avatar"
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
  /**
   * Начать новую беседу — кнопка рисуется **только** когда он передан.
   *
   * Необязательность здесь не «может быть, а может и не быть»: проп
   * отсутствует ровно у того вызывающего, у которого нет способа беседу
   * создать. До `G3-007-1` таким был каждый — беседу заводила фикстура, и
   * кнопка без следствия была бы указателем в никуда. `ChatPage` передаёт её,
   * и кнопка появляется; второй вызывающий появится — ему тоже понадобится
   * способ создать беседу, иначе он нарисует кнопку в пустоту.
   */
  onNewConversation?: () => void
  /**
   * Свёрнутая узкая колонка: аватары вместо строк. Только широкий экран — на
   * телефоне список занимает экран целиком, и `ChatPage` сюда `true` не передаёт.
   */
  collapsed?: boolean
  /** Свернуть или развернуть. Нет обработчика — нет и кнопки (как у остальных). */
  onToggleCollapsed?: () => void
}

export function ConversationSidebar({
  conversations,
  activeConversationId,
  currentUser,
  onSelectConversation,
  onNewConversation,
  collapsed = false,
  onToggleCollapsed,
}: ConversationSidebarProps) {
  if (collapsed) {
    return (
      <aside
        data-sidebar="collapsed"
        className="flex w-[76px] flex-shrink-0 flex-col items-center justify-between bg-surface-container-low shadow-[2px_0_16px_rgba(41,37,36,0.03)]"
      >
        <div className="flex min-h-0 w-full flex-1 flex-col items-center gap-2 py-4">
          {onToggleCollapsed && (
            <button
              type="button"
              aria-label="Expand chats panel"
              title="Expand chats panel"
              onClick={onToggleCollapsed}
              className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-accent-terracotta text-on-primary shadow-sm transition-all hover:bg-status-error active:scale-95"
            >
              <Icon name="chevron_right" size={22} />
            </button>
          )}
          {onNewConversation && (
            <button
              type="button"
              aria-label="New conversation"
              title="Start new chat"
              onClick={onNewConversation}
              className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-surface-cream text-accent-terracotta shadow-[0_2px_6px_rgba(41,37,36,0.04)] transition-all hover:bg-surface-warm-subtle active:scale-95"
            >
              <Icon name="edit_square" size={20} />
            </button>
          )}
          <nav className="mt-2 flex min-h-0 w-full flex-1 flex-col items-center gap-2 overflow-y-auto px-2">
            {conversations.map((conversation) => {
              const active = conversation.id === activeConversationId
              return (
                <button
                  key={conversation.id}
                  type="button"
                  // Те же атрибуты, что у строки списка: приёмка находит беседу одним адресом.
                  data-conversation-id={conversation.id}
                  data-unread-count={conversation.unreadCount}
                  aria-label={conversation.name}
                  title={conversation.name}
                  onClick={() => onSelectConversation(conversation.id)}
                  className={`relative flex-shrink-0 rounded-full p-0.5 transition-all active:scale-95 ${
                    active ? "ring-2 ring-accent-terracotta" : "hover:bg-surface-cream"
                  }`}
                >
                  <Avatar
                    name={conversation.name}
                    src={conversation.avatarUrl}
                    initials={conversation.initials}
                    size="md"
                    presence={conversation.presence}
                    ringClassName="ring-surface-container-low"
                  />
                  {Boolean(conversation.unreadCount) && (
                    <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-accent-terracotta px-1 text-[11px] font-semibold text-on-primary shadow-sm">
                      {conversation.unreadCount}
                    </span>
                  )}
                </button>
              )
            })}
          </nav>
        </div>
        <div className="flex-shrink-0 pb-4" title={currentUser.name}>
          <Avatar name={currentUser.name} src={currentUser.avatarUrl} ringClassName="ring-surface-container-low" />
        </div>
      </aside>
    )
  }

  return (
    <aside className="relative flex w-full flex-shrink-0 flex-col justify-between bg-surface-container-low shadow-[2px_0_16px_rgba(41,37,36,0.03)] md:w-[360px]">
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex-shrink-0">
        <div className="flex h-14 items-center justify-between px-3 md:h-20 md:px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-terracotta text-on-primary shadow-sm">
              <Icon name="forum" size={22} />
            </div>
            <div>
              <span className="text-lg font-semibold tracking-tight text-on-surface">Vector</span>
              <p className="hidden text-xs text-text-warm-muted md:block">Messages &amp; People</p>
            </div>
          </div>
          {onToggleCollapsed && (
            <button
              type="button"
              aria-label="Collapse chats panel"
              title="Collapse chats panel"
              onClick={onToggleCollapsed}
              className="ml-auto mr-2 hidden h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl text-text-warm-secondary transition-colors hover:bg-surface-warm-subtle hover:text-on-surface active:scale-95 md:flex"
            >
              <Icon name="chevron_left" size={22} />
            </button>
          )}
          {onNewConversation && (
            <button
              type="button"
              aria-label="New conversation"
              title="Start new chat"
              onClick={onNewConversation}
              className="group hidden h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl md:flex bg-surface-cream text-on-surface shadow-[0_2px_6px_rgba(41,37,36,0.04)] transition-all hover:bg-surface-warm-subtle active:scale-95"
            >
              <Icon
                name="edit_square"
                size={20}
                className="text-accent-terracotta transition-transform duration-200 group-hover:rotate-45"
              />
            </button>
          )}
        </div>
        </div>

        {/*
          `SearchField` здесь не рендерится, и это утверждение, а не пропуск.
          Поле — работающий локальный фильтр по **загруженному** массиву, а
          `GET /conversations` пагинирован, и пагинацию G3-005 намеренно не
          реализует: поле фильтровало бы первую страницу, обещая поиск по
          беседам вообще. Компонент и его stories остаются проектным запасом —
          поле вернётся тем срезом, который принесёт пагинацию.
        */}

        <div className="flex items-center justify-between px-4 py-1 md:px-6">
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

        <nav className="mt-1 flex-1 space-y-1.5 overflow-y-auto px-3 pb-24 md:space-y-1 md:px-2 md:pb-0">
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

      {/*
        Только узкий экран: на широком та же кнопка стоит в шапке. Плавающая, как в
        шаблоне, и над подвалом с профилем; вырез жестовой полосы учтён.
      */}
      {onNewConversation && (
        <button
          type="button"
          aria-label="Start new conversation"
          onClick={onNewConversation}
          className="group absolute bottom-24 right-4 z-30 flex h-14 items-center gap-2 rounded-full bg-accent-terracotta px-4 text-sm font-bold tracking-wide text-on-primary shadow-[0_8px_20px_rgba(234,88,12,0.35)] transition-all hover:bg-status-error active:scale-95 md:hidden"
        >
          <Icon name="edit_square" size={24} />
          New Chat
        </button>
      )}
    </aside>
  )
}
