import { Fragment, useEffect, useRef } from "react"
import type { ChatMessage } from "../../types"
import { DayDivider } from "../molecules/DayDivider"
import { DeletedMessageNotice } from "../molecules/DeletedMessageNotice"
import { EmptyConversationState } from "../molecules/EmptyConversationState"
import { MessageBubble } from "../molecules/MessageBubble"
import { SyncIndicator } from "../molecules/SyncIndicator"
import { TypingIndicator } from "../molecules/TypingIndicator"

interface MessageTimelineProps {
  dayLabel: string
  conversationName: string
  messages: ChatMessage[]
  syncIndicatorAfterMessageId?: string
  typingNames?: string[]
  onRetryMessage?: (id: string) => void
}

export function MessageTimeline({
  dayLabel,
  conversationName,
  messages,
  syncIndicatorAfterMessageId,
  typingNames = [],
  onRetryMessage,
}: MessageTimelineProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = containerRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, typingNames.length])

  if (messages.length === 0) {
    return <EmptyConversationState name={conversationName} />
  }

  return (
    <div ref={containerRef} className="mx-auto flex w-full max-w-4xl flex-1 flex-col space-y-4 overflow-y-auto px-6 py-4">
      <DayDivider label={dayLabel} />
      {messages.map((message) => (
        <Fragment key={message.id}>
          {message.deleted ? (
            <DeletedMessageNotice timestamp={message.timestamp} />
          ) : (
            <MessageBubble message={message} onRetry={onRetryMessage} />
          )}
          {syncIndicatorAfterMessageId === message.id && <SyncIndicator />}
        </Fragment>
      ))}
      <TypingIndicator names={typingNames} />
    </div>
  )
}
