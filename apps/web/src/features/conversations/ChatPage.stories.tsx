import type { Meta, StoryObj } from "@storybook/react-vite"
import { ChatPage } from "./ChatPage"
import { conversations, currentUser } from "../../shared/lib/mock-data"
import type { Conversation } from "../../shared/lib/types"

/**
 * Истории задают **данные**, а не состояние соединения.
 *
 * Пять прежних историй (`Connected`, `Reconnecting`, `Disconnected`,
 * `Degraded`, `Syncing`) различали пропс `initialConnectionState`, которого у
 * `ChatPage` больше нет: состояния соединения — предмет
 * `ConnectionStateBanner`, и все пять остались в его собственных историях
 * (`Messages/ConnectionStateBanner`). Здесь же показывается то, что главная
 * панель теперь различает сама: три состояния, ни одно из которых не лжёт.
 */

const withoutMessages = conversations.filter((c) => !c.hasMessages)

/** Беседа без `last_message`: превью пустое, и признак это подтверждает. */
const emptyConversation: Conversation = withoutMessages[0] ?? { ...conversations[0], hasMessages: false, lastMessagePreview: "", lastMessageTimestamp: undefined }

const meta: Meta<typeof ChatPage> = {
  title: "Conversations/ChatPage",
  component: ChatPage,
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof ChatPage>

/** Полный список; выбрана первая беседа, и история её не загружена. */
export const Default: Story = { args: { conversations, currentUser } }

/** Сервер вернул ноль бесед — это ответ, а не ошибка: шапки нет, список пуст. */
export const NoConversations: Story = { args: { conversations: [], currentUser } }

/** Выбранная беседа без последнего сообщения — единственный случай «No messages yet». */
export const NoMessages: Story = { args: { conversations: [emptyConversation], currentUser } }

/** Выбранная беседа с последним сообщением — тело говорит о незагруженной истории. */
export const WithMessages: Story = {
  args: { conversations: [conversations[0]], currentUser },
}
