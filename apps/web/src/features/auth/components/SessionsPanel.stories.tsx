import type { Meta, StoryObj } from "@storybook/react-vite"
import { SessionsPanel } from "./SessionsPanel"
import { deviceSessions } from "../../../shared/lib/mock-data"

const meta: Meta<typeof SessionsPanel> = {
  title: "Auth/SessionsPanel",
  component: SessionsPanel,
  args: { sessions: deviceSessions, onLogOut: () => {}, onLogOutEverywhere: () => {} },
}
export default meta
type Story = StoryObj<typeof SessionsPanel>

export const Default: Story = {}
