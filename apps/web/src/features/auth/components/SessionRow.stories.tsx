import type { Meta, StoryObj } from "@storybook/react-vite"
import { SessionRow } from "./SessionRow"

const meta: Meta<typeof SessionRow> = {
  title: "Auth/SessionRow",
  component: SessionRow,
  args: { onLogOut: () => {} },
  decorators: [(Story) => <div className="w-96 p-4"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof SessionRow>

export const CurrentDevice: Story = {
  args: { session: { id: "s1", deviceLabel: "MacBook Pro · Chrome", userAgent: "macOS 15", lastSeenAt: "now", current: true } },
}
export const OtherDevice: Story = {
  args: { session: { id: "s2", deviceLabel: "iPhone 16 · Vector app", userAgent: "iOS 18", lastSeenAt: "12 minutes ago" } },
}
