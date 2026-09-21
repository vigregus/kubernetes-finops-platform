import type { Meta, StoryObj } from "@storybook/react-vite"
import { ConversationListItem } from "./ConversationListItem"

const meta: Meta<typeof ConversationListItem> = {
  title: "Conversations/ConversationListItem",
  component: ConversationListItem,
  decorators: [(Story) => <div className="w-80 bg-surface-container-low p-2"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof ConversationListItem>

const base = {
  id: "1",
  name: "Anna Petrova",
  presence: "online" as const,
  lastMessagePreview: "That works! Let's review the finalized slides tomorrow.",
  lastMessageTimestamp: "14:22",
  // Признак идёт парой с остальными данными: фикстура изображает беседу, о
  // последнем сообщении которой сервер сообщил. У истории ниже он меняется
  // вместе с ними, а не отдельно.
  hasMessages: true,
}

export const Active: Story = { args: { conversation: base, active: true, onSelect: () => {} } }
export const Inactive: Story = { args: { conversation: base, active: false, onSelect: () => {} } }
export const Unread: Story = {
  args: { conversation: { ...base, name: "Marcus Chen", unreadCount: 2, presence: "away" }, onSelect: () => {} },
}
export const Typing: Story = {
  args: { conversation: { ...base, typingNames: ["Marcus"] }, onSelect: () => {} },
}
export const DeletedPreview: Story = {
  args: { conversation: { ...base, name: "Daniel Kim", lastMessagePreview: "Message deleted", previewDeleted: true }, onSelect: () => {} },
}
export const NoMessagesYet: Story = {
  args: {
    conversation: {
      ...base,
      name: "New Hire Onboarding",
      lastMessagePreview: "",
      lastMessageTimestamp: "",
      // Единственная история, где сервер сообщил, что последнего сообщения нет.
      hasMessages: false,
    },
    onSelect: () => {},
  },
}
export const InitialsFallback: Story = {
  args: { conversation: { ...base, name: "Elena Rostova", initials: "ER" }, onSelect: () => {} },
}
