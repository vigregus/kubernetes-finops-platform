import type { Meta, StoryObj } from "@storybook/react-vite"
import { TextField } from "./TextField"

const meta: Meta<typeof TextField> = {
  title: "Shared UI/TextField",
  component: TextField,
  args: { placeholder: "Type something..." },
  decorators: [(Story) => <div className="w-72 rounded-xl bg-surface-cream p-3"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof TextField>

export const Empty: Story = {}
export const Filled: Story = { args: { defaultValue: "Hello there" } }
export const Disabled: Story = { args: { disabled: true, placeholder: "Disabled" } }
