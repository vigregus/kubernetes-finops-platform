import type { Meta, StoryObj } from "@storybook/react-vite"
import { LoginPage } from "./LoginPage"

const meta: Meta<typeof LoginPage> = {
  title: "Pages/LoginPage",
  component: LoginPage,
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof LoginPage>

export const Default: Story = {}
export const SessionExpired: Story = { args: { sessionExpired: true } }
