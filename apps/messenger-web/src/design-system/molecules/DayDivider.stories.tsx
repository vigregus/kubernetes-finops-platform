import type { Meta, StoryObj } from "@storybook/react-vite"
import { DayDivider } from "./DayDivider"

const meta: Meta<typeof DayDivider> = {
  title: "Molecules/DayDivider",
  component: DayDivider,
  args: { label: "Today" },
  decorators: [(Story) => <div className="max-w-md p-6"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof DayDivider>

export const Today: Story = {}
export const SpecificDate: Story = { args: { label: "September 20" } }
