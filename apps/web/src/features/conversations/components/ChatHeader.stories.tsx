import type { Meta, StoryObj } from "@storybook/react-vite"
import { ChatHeader } from "./ChatHeader"

const meta: Meta<typeof ChatHeader> = {
  title: "Conversations/ChatHeader",
  component: ChatHeader,
  decorators: [(Story) => <div className="w-full max-w-xl"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof ChatHeader>

const base = { id: "1", name: "Anna Petrova", lastMessagePreview: "", lastMessageTimestamp: "" }

export const Online: Story = { args: { conversation: { ...base, presence: "online" } } }
export const Away: Story = { args: { conversation: { ...base, presence: "away" } } }
export const LastSeen: Story = { args: { conversation: { ...base, presence: "offline", lastSeenAt: "9:14 AM" } } }
export const Typing: Story = { args: { conversation: { ...base, presence: "online", typingNames: ["Anna"] } } }
export const BlockedByMe: Story = { args: { conversation: { ...base, blockedByMe: true } } }
export const BlockedMe: Story = { args: { conversation: { ...base, blockedMe: true } } }
