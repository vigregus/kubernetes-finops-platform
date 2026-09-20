import type { Meta, StoryObj } from "@storybook/react-vite"
import { Badge } from "./Badge"

const meta: Meta<typeof Badge> = {
  title: "Shared UI/Badge",
  component: Badge,
  args: { count: 2 },
}
export default meta
type Story = StoryObj<typeof Badge>

export const Default: Story = {}
export const DoubleDigit: Story = { args: { count: 24 } }
