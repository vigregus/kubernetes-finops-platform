import type { Meta, StoryObj } from "@storybook/react-vite"
import { MessageComposer } from "./MessageComposer"

const meta: Meta<typeof MessageComposer> = {
  title: "Molecules/MessageComposer",
  component: MessageComposer,
  args: { recipientName: "Anna Petrova", onSend: () => {} },
  decorators: [(Story) => <div className="max-w-2xl bg-surface p-4"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof MessageComposer>

export const Default: Story = {}
export const Disabled: Story = {
  name: "Disabled — blocked (08-authorization.md symmetric block)",
  args: { disabled: true, disabledReason: "You can no longer message this person." },
}
