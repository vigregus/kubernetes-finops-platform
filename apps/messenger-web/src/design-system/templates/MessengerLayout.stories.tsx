import type { Meta, StoryObj } from "@storybook/react-vite"
import { MessengerLayout } from "./MessengerLayout"
import { ConversationSidebar } from "../organisms/ConversationSidebar"
import { ChatHeader } from "../organisms/ChatHeader"
import { MessageTimeline } from "../organisms/MessageTimeline"
import { MessageComposer } from "../molecules/MessageComposer"
import { conversations, currentUser, messagesByConversation } from "../../data/mockData"

const meta: Meta<typeof MessengerLayout> = {
  title: "Templates/MessengerLayout",
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
