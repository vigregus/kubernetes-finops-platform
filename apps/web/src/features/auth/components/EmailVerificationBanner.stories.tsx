import type { Meta, StoryObj } from "@storybook/react-vite"
import { EmailVerificationBanner } from "./EmailVerificationBanner"

const meta: Meta<typeof EmailVerificationBanner> = {
  title: "Auth/EmailVerificationBanner",
  component: EmailVerificationBanner,
  parameters: {
    docs: {
      description: {
        component:
          "AUTH-006: before email confirmation, START_CONVERSATION alone is denied — reading and replying in an existing conversation stay allowed.",
      },
    },
  },
  args: { email: "david.miller@example.com", resend: () => new Promise(() => {}) },
}
export default meta
type Story = StoryObj<typeof EmailVerificationBanner>

export const Default: Story = {}
export const ShortCooldown: Story = { args: { defaultCooldownSeconds: 5, resend: () => Promise.resolve() } }
