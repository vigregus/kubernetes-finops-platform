import { useState } from "react"
import { ConversationSidebar } from "./components/ConversationSidebar"
import { ChatHeader } from "./components/ChatHeader"
import { BlockedNotice } from "./components/BlockedNotice"
import { MessageTimeline } from "../messages/components/MessageTimeline"
import { ConnectionStateBanner } from "../messages/components/ConnectionStateBanner"
import { MessageComposer } from "../messages/components/MessageComposer"
import { MessengerLayout } from "../../shared/ui/MessengerLayout"
import {
  activeConversationId as initialConversationId,
  conversations,
  currentUser,
  messagesByConversation,
} from "../../shared/lib/mock-data"
import type { ChatMessage, ConnectionState } from "../../shared/lib/types"

interface ChatPageProps {
  initialConnectionState?: ConnectionState
}

export function ChatPage({ initialConnectionState = "connected" }: ChatPageProps) {
  const [activeId, setActiveId] = useState(initialConversationId)
  const [messagesById, setMessagesById] = useState(messagesByConversation)
  const [connectionState] = useState<ConnectionState>(initialConnectionState)

  const activeConversation = conversations.find((c) => c.id === activeId) ?? conversations[0]
  const messages = messagesById[activeId] ?? []
  const blocked = activeConversation.blockedByMe || activeConversation.blockedMe

  function handleSend(text: string) {
    const message: ChatMessage = {
      id: `local-${Date.now()}`,
      authorId: "me",
      kind: "text",
      text,
      timestamp: new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }),
      deliveryState: "sending",
    }
    setMessagesById((prev) => ({ ...prev, [activeId]: [...(prev[activeId] ?? []), message] }))
  }

  function handleRetry(id: string) {
    setMessagesById((prev) => ({
      ...prev,
      [activeId]: (prev[activeId] ?? []).map((m) => (m.id === id ? { ...m, deliveryState: "retrying" } : m)),
    }))
  }

  return (
    <MessengerLayout
      sidebar={
        <ConversationSidebar
          conversations={conversations}
          activeConversationId={activeId}
          currentUser={currentUser}
          onSelectConversation={setActiveId}
        />
      }
    >
      <ChatHeader conversation={activeConversation} />
      <ConnectionStateBanner state={connectionState} />
      <MessageTimeline
        dayLabel="Today"
        conversationName={activeConversation.name}
        messages={messages}
        syncIndicatorAfterMessageId="m5"
        typingNames={blocked ? [] : activeConversation.typingNames}
        onRetryMessage={handleRetry}
      />
      {activeConversation.blockedMe ? (
        <BlockedNotice blockedMe />
      ) : activeConversation.blockedByMe ? (
        <BlockedNotice blockedMe={false} onUnblock={() => {}} />
      ) : (
        <MessageComposer recipientName={activeConversation.name} onSend={handleSend} />
      )}
    </MessengerLayout>
  )
}
