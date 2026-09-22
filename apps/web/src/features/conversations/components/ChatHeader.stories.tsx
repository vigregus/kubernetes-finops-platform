import type { Meta, StoryObj } from "@storybook/react-vite"
import { ChatHeader } from "./ChatHeader"

const meta: Meta<typeof ChatHeader> = {
  title: "Conversations/ChatHeader",
  component: ChatHeader,
  decorators: [(Story) => <div className="w-full max-w-xl"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof ChatHeader>

// `hasMessages: false` — согласовано с пустым превью: фикстура изображает беседу
// без последнего сообщения, и признак говорит то же, что и превью. Шапка его не
// читает — он здесь потому, что без него `Conversation` не соберётся, и это
// намеренно: тип требует ответить на вопрос «сервер сообщил сообщение?», а не
// оставить его неотвеченным.
const base = {
  id: "1",
  name: "Anna Petrova",
  lastMessagePreview: "",
  lastMessageTimestamp: "",
  hasMessages: false,
}

export const Online: Story = { args: { conversation: { ...base, presence: "online" } } }
export const Away: Story = { args: { conversation: { ...base, presence: "away" } } }
export const LastSeen: Story = { args: { conversation: { ...base, presence: "offline", lastSeenAt: "9:14 AM" } } }
export const Typing: Story = { args: { conversation: { ...base, presence: "online", typingNames: ["Anna"] } } }
export const BlockedByMe: Story = { args: { conversation: { ...base, blockedByMe: true } } }
export const BlockedMe: Story = { args: { conversation: { ...base, blockedMe: true } } }
