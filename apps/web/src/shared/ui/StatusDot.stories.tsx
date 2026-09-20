import type { Meta, StoryObj } from "@storybook/react-vite"
import { StatusDot } from "./StatusDot"

const meta: Meta<typeof StatusDot> = {
  title: "Shared UI/StatusDot",
  component: StatusDot,
  decorators: [(Story) => <div className="relative h-6 w-6 rounded-full bg-surface-container-high"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof StatusDot>

export const Online: Story = { args: { presence: "online" } }
export const Away: Story = { args: { presence: "away" } }
export const Offline: Story = { args: { presence: "offline" } }
