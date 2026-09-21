import { useState } from "react"
import { ConversationSidebar } from "./components/ConversationSidebar"
import { ChatHeader } from "./components/ChatHeader"
import { EmptyConversationState } from "../messages/components/EmptyConversationState"
import { MessengerLayout } from "../../shared/ui/MessengerLayout"
import type { Conversation, CurrentUser } from "../../shared/lib/types"

interface ChatPageProps {
  /** Беседы из `GET /conversations`. Фикстур здесь нет и быть не может. */
  conversations: Conversation[]
  currentUser: CurrentUser
}

/**
 * Главная панель знает **три** состояния, а не два, и ни одно из них не лжёт.
 *
 * Разделение обязательно, потому что `EmptyConversationState` говорит «No
 * messages yet. Say hello to {name}» — и это правда **только** при отсутствии
 * `last_message`. Если `GET /conversations` вернул `last_message`, сообщения
 * есть; G3-005 их не загружает, но сказать «их нет» значило бы утверждать
 * обратное тому, что сообщил сервер — тот же класс, что «Active now» и «Last
 * seen». Третье состояние и есть способ честно не реализовать историю там, где
 * `Scope` её прямо запрещает.
 *
 * `MessageTimeline`, `MessageComposer`, `ConnectionStateBanner` и `BlockedNotice`
 * сюда не подключены: они остаются проектным запасом, но в production-путь
 * G3-005 не входят (закрытый список B15).
 */
export function ChatPage({ conversations, currentUser }: ChatPageProps) {
  // Ленивая инициализация, а не `?? conversations[0]` в рендере: запасного
  // значения у настоящих данных нет, а пустой список — законный ответ сервера.
  const [activeId, setActiveId] = useState<string | null>(() => conversations[0]?.id ?? null)

  const activeConversation = conversations.find((c) => c.id === activeId) ?? null

  return (
    <MessengerLayout
      sidebar={
        <ConversationSidebar
          conversations={conversations}
          activeConversationId={activeConversation?.id ?? null}
          currentUser={currentUser}
          onSelectConversation={setActiveId}
        />
      }
    >
      {activeConversation === null ? (
        // Шапки нет: шапка — утверждение о выбранной беседе, а её нет.
        <div className="flex flex-1 items-center justify-center px-6 text-center">
          <p className="text-sm text-text-warm-secondary">No conversations</p>
        </div>
      ) : (
        <>
          <ChatHeader conversation={activeConversation} />
          {activeConversation.hasMessages ? (
            <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
              <p className="text-sm text-text-warm-secondary">Message history isn&apos;t loaded yet.</p>
            </div>
          ) : (
            <EmptyConversationState name={activeConversation.name} />
          )}
        </>
      )}
    </MessengerLayout>
  )
}
