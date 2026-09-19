import type { Meta, StoryObj } from "@storybook/react-vite"
import { CurrentUserFooter } from "./CurrentUserFooter"

const meta: Meta<typeof CurrentUserFooter> = {
  title: "Molecules/CurrentUserFooter",
  component: CurrentUserFooter,
  args: { user: { name: "David Miller", handle: "@dmiller.signal", presence: "online" } },
  decorators: [(Story) => <div className="w-80 bg-surface-container-low p-3"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof CurrentUserFooter>

export const Default: Story = {}
