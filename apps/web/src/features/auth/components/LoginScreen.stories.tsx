import type { Meta, StoryObj } from "@storybook/react-vite"
import { LoginScreen } from "./LoginScreen"

const meta: Meta<typeof LoginScreen> = {
  title: "Auth/LoginScreen",
  component: LoginScreen,
  args: { onLogin: () => {} },
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof LoginScreen>

export const Default: Story = {}
export const SessionExpired: Story = { name: "Session expired (AUTH-004)", args: { sessionExpired: true } }
