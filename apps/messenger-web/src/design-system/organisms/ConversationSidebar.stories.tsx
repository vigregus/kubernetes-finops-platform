import type { Meta, StoryObj } from "@storybook/react-vite"
import { ConversationSidebar } from "./ConversationSidebar"
import { conversations, currentUser } from "../../data/mockData"

const meta: Meta<typeof ConversationSidebar> = {
  title: "Organisms/ConversationSidebar",
  component: ConversationSidebar,
  args: {
    conversations,
    activeConversationId: conversations[0].id,
    currentUser,
    onSelectConversation: () => {},
  },
  decorators: [(Story) => <div className="flex h-[640px]"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof ConversationSidebar>

export const Default: Story = {}
export const EmptyList: Story = { args: { conversations: [] } }
