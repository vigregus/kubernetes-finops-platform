import type { Meta, StoryObj } from "@storybook/react-vite"
import { UnsupportedMessageNotice } from "./UnsupportedMessageNotice"

const meta: Meta<typeof UnsupportedMessageNotice> = {
  title: "Messages/UnsupportedMessageNotice",
  component: UnsupportedMessageNotice,
  parameters: { docs: { description: { component: "CLI-001: a message type from a newer client must still occupy its seq slot, not be silently skipped." } } },
  args: { timestamp: "10:39 AM" },
  decorators: [(Story) => <div className="max-w-md p-6"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof UnsupportedMessageNotice>

export const Incoming: Story = { args: { isOwn: false } }
export const Outgoing: Story = { args: { isOwn: true } }
