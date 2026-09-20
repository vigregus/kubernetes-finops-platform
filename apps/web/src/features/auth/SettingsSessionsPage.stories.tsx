import type { Meta, StoryObj } from "@storybook/react-vite"
import { SettingsSessionsPage } from "./SettingsSessionsPage"

const meta: Meta<typeof SettingsSessionsPage> = {
  title: "Auth/SettingsSessionsPage",
  component: SettingsSessionsPage,
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof SettingsSessionsPage>

export const Default: Story = {}
