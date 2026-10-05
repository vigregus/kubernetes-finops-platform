import type { Meta, StoryObj } from "@storybook/react-vite"
import { NotificationPrompt } from "./NotificationPrompt"

const meta: Meta<typeof NotificationPrompt> = {
  title: "Notifications/NotificationPrompt",
  component: NotificationPrompt,
  parameters: {
    docs: {
      description: {
        component:
          "NTF-001, NTF-005: offer Web Push only when there is something to offer; a browser-level block is explained once and the app keeps working.",
      },
    },
  },
  args: { onTurnOn: () => {} },
}
export default meta
type Story = StoryObj<typeof NotificationPrompt>

export const Offer: Story = { args: { state: { kind: "off" } } }
export const Working: Story = { args: { state: { kind: "working" } } }
export const BlockedInBrowser: Story = { args: { state: { kind: "denied" } } }
export const Failed: Story = { args: { state: { kind: "error", message: "Couldn't turn on notifications. Try again." } } }
export const Subscribed: Story = { name: "Subscribed (renders nothing)", args: { state: { kind: "on" } } }
export const Unsupported: Story = { name: "Unsupported (renders nothing)", args: { state: { kind: "unsupported" } } }
