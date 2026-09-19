import type { Meta, StoryObj } from "@storybook/react-vite"
import { BlockedNotice } from "./BlockedNotice"

const meta: Meta<typeof BlockedNotice> = {
  title: "Molecules/BlockedNotice",
  component: BlockedNotice,
  parameters: { docs: { description: { component: "08-authorization.md — symmetric block: neither side can write; unblocking is an explicit action." } } },
  decorators: [(Story) => <div className="bg-surface"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof BlockedNotice>

export const TheyBlockedYou: Story = { args: { blockedMe: true } }
export const YouBlockedThem: Story = { args: { blockedMe: false, onUnblock: () => {} } }
