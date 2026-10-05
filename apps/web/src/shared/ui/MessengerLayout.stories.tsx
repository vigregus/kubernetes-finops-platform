import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"
import { MessengerLayout } from "./MessengerLayout"
import { ConversationSidebar } from "../../features/conversations/components/ConversationSidebar"
import { ChatHeader } from "../../features/conversations/components/ChatHeader"
import { MessageTimeline } from "../../features/messages/components/MessageTimeline"
import { MessageComposer } from "../../features/messages/components/MessageComposer"
import { conversations, currentUser, messagesByConversation } from "../lib/mock-data"

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

function screens() {
  return {
    sidebar: (
      <ConversationSidebar
        conversations={conversations}
        activeConversationId={null}
        currentUser={currentUser}
        onSelectConversation={() => {}}
        onNewConversation={() => {}}
      />
    ),
    chat: (
      <>
        <ChatHeader conversation={conversations[0]} onBack={() => {}} />
        <MessageTimeline dayLabel="Today" conversationName="Anna Petrova" messages={messagesByConversation["anna-petrova"]} />
        <MessageComposer recipientName="Anna Petrova" onSend={() => {}} />
      </>
    ),
  }
}

/** Телефон, экран «список»: беседа не рисуется вовсе (а не прячется стилем). */
export const MobileList: Story = {
  globals: { viewport: { value: "phone", isRotated: false } },
  render: () => {
    const { sidebar, chat } = screens()
    return <MessengerLayout screen="list" sidebar={sidebar}>{chat}</MessengerLayout>
  },
  play: async ({ canvasElement }) => {
    await expect(canvasElement.querySelector("main")).toBeNull()
    await expect(canvasElement.querySelector("aside")).not.toBeNull()
  },
}

/** Телефон, экран «беседа»: списка нет, шапка со стрелкой «назад». */
export const MobileChat: Story = {
  globals: { viewport: { value: "phone", isRotated: false } },
  render: () => {
    const { sidebar, chat } = screens()
    return <MessengerLayout screen="chat" sidebar={sidebar}>{chat}</MessengerLayout>
  },
  play: async ({ canvasElement }) => {
    await expect(canvasElement.querySelector("aside")).toBeNull()
    await expect(canvasElement.querySelector("main")).not.toBeNull()
  },
}
