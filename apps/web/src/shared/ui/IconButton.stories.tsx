import type { Meta, StoryObj } from "@storybook/react-vite"
import { IconButton } from "./IconButton"

const meta: Meta<typeof IconButton> = {
  title: "Atoms/IconButton",
  component: IconButton,
  args: { icon: "search", label: "Search" },
}
export default meta
type Story = StoryObj<typeof IconButton>

export const Ghost: Story = { args: { variant: "ghost" } }
export const Filled: Story = { args: { variant: "filled", icon: "edit_square", label: "New conversation" } }
