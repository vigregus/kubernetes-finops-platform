import type { Meta, StoryObj } from "@storybook/react-vite"
import { MessageTimeline } from "./MessageTimeline"
import { messagesByConversation } from "../../../shared/lib/mock-data"

const meta: Meta<typeof MessageTimeline> = {
  title: "Messages/MessageTimeline",
  component: MessageTimeline,
  args: { dayLabel: "Today", conversationName: "Anna Petrova" },
  decorators: [(Story) => <div className="flex h-[640px] bg-surface"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof MessageTimeline>

export const FullConversation: Story = {
  args: { messages: messagesByConversation["anna-petrova"], syncIndicatorAfterMessageId: "m5" },
}
export const WithTypingIndicator: Story = {
  args: { messages: messagesByConversation["marcus-chen"], typingNames: ["Marcus"] },
}
export const Empty: Story = { name: "Empty (LIST-003)", args: { messages: [] } }
