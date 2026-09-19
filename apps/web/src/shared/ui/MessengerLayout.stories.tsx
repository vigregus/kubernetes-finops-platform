import type { Meta, StoryObj } from "@storybook/react-vite"
import { MessengerLayout } from "./MessengerLayout"
import { ConversationSidebar } from "../../features/conversations/components/ConversationSidebar"
import { ChatHeader } from "../../features/conversations/components/ChatHeader"
import { MessageTimeline } from "../../features/messages/components/MessageTimeline"
import { MessageComposer } from "../../features/messages/components/MessageComposer"
import { conversations, currentUser, messagesByConversation } from "../lib/mock-data"

const meta: Meta<typeof MessengerLayout> = {
  title: "Shared UI/MessengerLayout",
  component: MessengerLayout,
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof MessengerLayout>

export const Default: Story = {
  render: () => (
    <MessengerLayout
      sidebar={
        <ConversationSidebar
          conversations={conversations}
          activeConversationId="anna-petrova"
          currentUser={currentUser}
          onSelectConversation={() => {}}
        />
      }
    >
      <ChatHeader conversation={conversations[0]} />
      <MessageTimeline dayLabel="Today" conversationName="Anna Petrova" messages={messagesByConversation["anna-petrova"]} syncIndicatorAfterMessageId="m5" />
      <MessageComposer recipientName="Anna Petrova" onSend={() => {}} />
    </MessengerLayout>
  ),
}
