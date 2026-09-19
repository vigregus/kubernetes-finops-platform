import type { Meta, StoryObj } from "@storybook/react-vite"
import { SyncIndicator } from "./SyncIndicator"

const meta: Meta<typeof SyncIndicator> = {
  title: "Molecules/SyncIndicator",
  component: SyncIndicator,
  decorators: [(Story) => <div className="max-w-md p-6"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof SyncIndicator>

export const Default: Story = {}
