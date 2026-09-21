import type { Meta, StoryObj } from "@storybook/react-vite"
import { CurrentUserFooter } from "./CurrentUserFooter"

const meta: Meta<typeof CurrentUserFooter> = {
  title: "Auth/CurrentUserFooter",
  component: CurrentUserFooter,
  args: { user: { name: "David Miller", email: "david.miller@example.com", emailVerified: true } },
  decorators: [(Story) => <div className="w-80 bg-surface-container-low p-3"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof CurrentUserFooter>

export const Default: Story = {}

/**
 * С обработчиком настроек — кнопка есть. Без него (история `Default`) её нет:
 * элемент без обработчика не рендерится, а не рисуется нерабочим.
 */
export const WithSettings: Story = { args: { onOpenSettings: () => {} } }
