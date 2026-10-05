import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, within } from "storybook/test"
import { ConversationSidebar } from "./ConversationSidebar"
import { conversations, currentUser } from "../../../shared/lib/mock-data"

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

/** Телефон: список на весь экран, «New Chat» плавает над профилем (на широком кнопка в шапке). */
export const Mobile: Story = {
  args: { onNewConversation: () => {}, activeConversationId: null },
  globals: { viewport: { value: "phone", isRotated: false } },
  decorators: [(Story) => <div className="flex h-dvh"><Story /></div>],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole("button", { name: "Start new conversation" })).toBeVisible()
    // Карточки без подсветки «активной»: беседа открывается на весь экран.
    await expect(canvasElement.querySelectorAll("[data-conversation-id]").length).toBeGreaterThan(0)
  },
}
