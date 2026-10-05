import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn, userEvent, within } from "storybook/test"
import { ChatHeader } from "./ChatHeader"

const meta: Meta<typeof ChatHeader> = {
  title: "Molecules/ChatHeader",
  component: ChatHeader,
  decorators: [(Story) => <div className="w-full max-w-xl"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof ChatHeader>

// `hasMessages: false` — согласовано с пустым превью: фикстура изображает беседу
// без последнего сообщения, и признак говорит то же, что и превью. Шапка его не
// читает — он здесь потому, что без него `Conversation` не соберётся, и это
// намеренно: тип требует ответить на вопрос «сервер сообщил сообщение?», а не
// оставить его неотвеченным.
const base = {
  id: "1",
  name: "Anna Petrova",
  lastMessagePreview: "",
  lastMessageTimestamp: "",
  hasMessages: false,
}

export const Online: Story = { args: { conversation: { ...base, presence: "online" } } }
export const Away: Story = { args: { conversation: { ...base, presence: "away" } } }
export const LastSeen: Story = { args: { conversation: { ...base, presence: "offline", lastSeenAt: "9:14 AM" } } }
export const Typing: Story = { args: { conversation: { ...base, presence: "online", typingNames: ["Anna"] } } }
export const BlockedByMe: Story = { args: { conversation: { ...base, blockedByMe: true } } }
export const BlockedMe: Story = { args: { conversation: { ...base, blockedMe: true } } }

/** Телефон: стрелка «назад» к списку бесед, шапка ниже выреза экрана. */
export const MobileWithBack: Story = {
  args: { conversation: { ...base, presence: "online" }, onBack: fn(), onVoiceCall: () => {}, onVideoCall: () => {} },
  globals: { viewport: { value: "phone", isRotated: false } },
  play: async ({ canvasElement, args }) => {
    const canvas = within(canvasElement)
    await expect(canvas.getByRole("button", { name: "Start voice call" })).toBeVisible()
    await userEvent.click(canvas.getByRole("button", { name: "Back to chats" }))
    await expect(args.onBack).toHaveBeenCalledOnce()
  },
}

/** Без `onBack` стрелки нет: на широком экране список рядом. */
export const NoBackOnDesktop: Story = {
  args: { conversation: { ...base, presence: "online" } },
  play: async ({ canvasElement }) => {
    await expect(within(canvasElement).queryByRole("button", { name: "Back to chats" })).toBeNull()
  },
}

/** Звонки включены, но недоступны (нет защищённого соединения или связи): кнопки выключены, причина в подсказке. */
export const CallsUnavailable: Story = {
  args: { conversation: { ...base, presence: "online" }, callUnavailableReason: "Connecting to the call service…" },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const voice = canvas.getByRole("button", { name: "Start voice call" })
    await expect(voice).toBeDisabled()
    await expect(voice).toHaveAttribute("title", "Connecting to the call service…")
  },
}
