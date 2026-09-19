import type { Meta, StoryObj } from "@storybook/react-vite"
import { DeletedMessageNotice } from "./DeletedMessageNotice"

const meta: Meta<typeof DeletedMessageNotice> = {
  title: "Messages/DeletedMessageNotice",
  component: DeletedMessageNotice,
  parameters: { docs: { description: { component: "09-data-lifecycle.md: deletion is a tombstone (deleted_at set, payload nulled), not a physical delete — ordering and unread counts stay consistent." } } },
  args: { timestamp: "10:38 AM" },
  decorators: [(Story) => <div className="max-w-md p-6"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof DeletedMessageNotice>

export const Default: Story = {}
