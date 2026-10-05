import type { Meta, StoryObj } from "@storybook/react-vite"
import { ConnectionStateBanner } from "./ConnectionStateBanner"

const meta: Meta<typeof ConnectionStateBanner> = {
  title: "Molecules/ConnectionStateBanner",
  component: ConnectionStateBanner,
  parameters: {
    docs: {
      description: {
        component:
          '03-v1-scope.md "Веб-клиент: состояния соединения" — CONNECTED / CONNECTING / DISCONNECTED / DEGRADED / SYNCING. recovered=true/false alone is not enough; never show stale state as current while syncing.',
      },
    },
  },
  decorators: [(Story) => <div className="bg-surface p-4"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof ConnectionStateBanner>

export const Connected: Story = { name: "Connected (hidden)", args: { state: "connected" } }
export const Connecting: Story = { args: { state: "connecting" } }
export const Disconnected: Story = { args: { state: "disconnected" } }
export const Degraded: Story = { name: "Degraded (typing/presence paused)", args: { state: "degraded" } }
export const Syncing: Story = { name: "Syncing (recovered=false, replaying history)", args: { state: "syncing" } }
