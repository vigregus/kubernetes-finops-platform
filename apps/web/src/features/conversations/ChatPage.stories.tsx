import type { Meta, StoryObj } from "@storybook/react-vite"
import { ChatPage } from "./ChatPage"

const meta: Meta<typeof ChatPage> = {
  title: "Conversations/ChatPage",
  component: ChatPage,
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof ChatPage>

export const Connected: Story = {}
export const Reconnecting: Story = { args: { initialConnectionState: "connecting" } }
export const Disconnected: Story = { args: { initialConnectionState: "disconnected" } }
export const Degraded: Story = { args: { initialConnectionState: "degraded" } }
export const Syncing: Story = { args: { initialConnectionState: "syncing" } }
