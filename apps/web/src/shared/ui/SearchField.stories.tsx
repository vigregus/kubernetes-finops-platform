import { useState } from "react"
import type { Meta, StoryObj } from "@storybook/react-vite"
import { SearchField } from "./SearchField"

const meta: Meta<typeof SearchField> = {
  title: "Shared UI/SearchField",
  component: SearchField,
  args: { placeholder: "Search chats..." },
  decorators: [(Story) => <div className="w-80 p-4"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof SearchField>

export const Interactive: Story = {
  render: (args) => {
    function Controlled() {
      const [value, setValue] = useState("")
      return <SearchField {...args} value={value} onChange={setValue} />
    }
    return <Controlled />
  },
}
