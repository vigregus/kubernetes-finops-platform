import type { Meta, StoryObj } from "@storybook/react-vite"
import { EmailVerificationBanner } from "./EmailVerificationBanner"

const meta: Meta<typeof EmailVerificationBanner> = {
  title: "Auth/EmailVerificationBanner",
  component: EmailVerificationBanner,
  parameters: { docs: { description: { component: "AUTH-006: before email confirmation, functionality is limited; resend is rate-limited." } } },
  args: { email: "david.miller@example.com", onResend: () => {} },
}
export default meta
type Story = StoryObj<typeof EmailVerificationBanner>

export const Default: Story = {}
export const ShortCooldown: Story = { args: { resendCooldownSeconds: 5 } }
