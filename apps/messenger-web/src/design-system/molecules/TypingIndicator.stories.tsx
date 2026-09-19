import type { Meta, StoryObj } from "@storybook/react-vite"
import { TypingIndicator } from "./TypingIndicator"

const meta: Meta<typeof TypingIndicator> = {
  title: "Molecules/TypingIndicator",
  component: TypingIndicator,
  parameters: { docs: { description: { component: "RT-001..004: receiver-side indicator auto-clears 4-5s without a heartbeat; group label scales with participant count." } } },
  decorators: [(Story) => <div className="max-w-md p-6"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof TypingIndicator>

export const OnePerson: Story = { args: { names: ["Anna"] } }
export const TwoPeople: Story = { args: { names: ["Anna", "Marcus"] } }
export const ThreeOrMore: Story = { args: { names: ["Anna", "Marcus", "Daniel"] } }
export const Nobody: Story = { name: "Nobody typing (renders nothing)", args: { names: [] } }
