import type { Meta, StoryObj } from "@storybook/react-vite"
import { SessionsPanel } from "./SessionsPanel"
import { deviceSessions } from "../../data/mockData"

const meta: Meta<typeof SessionsPanel> = {
  title: "Organisms/SessionsPanel",
  component: SessionsPanel,
  args: { sessions: deviceSessions, onLogOut: () => {}, onLogOutEverywhere: () => {} },
}
export default meta
type Story = StoryObj<typeof SessionsPanel>

export const Default: Story = {}
