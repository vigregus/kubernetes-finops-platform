import type { Meta, StoryObj } from "@storybook/react-vite"
import { EmptyConversationState } from "./EmptyConversationState"

const meta: Meta<typeof EmptyConversationState> = {
  title: "Molecules/EmptyConversationState",
  component: EmptyConversationState,
  parameters: { docs: { description: { component: "LIST-003: a conversation can exist with zero messages — no crash, no fake preview." } } },
  args: { name: "New Hire Onboarding" },
  decorators: [(Story) => <div className="flex h-72 bg-surface"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof EmptyConversationState>

export const Default: Story = {}
