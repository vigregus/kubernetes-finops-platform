import type { Meta, StoryObj } from "@storybook/react-vite"
import { UpdateAvailableBanner } from "./UpdateAvailableBanner"

const meta: Meta<typeof UpdateAvailableBanner> = {
  title: "Molecules/UpdateAvailableBanner",
  component: UpdateAvailableBanner,
  parameters: { docs: { description: { component: "13-client-compatibility.md Часть 2/5: incompatible Service Worker update — reload deferred until the user isn't typing, or user is prompted." } } },
  args: { onReloadNow: () => {} },
}
export default meta
type Story = StoryObj<typeof UpdateAvailableBanner>

export const Default: Story = {}
