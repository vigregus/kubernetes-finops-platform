import type { Meta, StoryObj } from "@storybook/react-vite"
import { ChatPage } from "./ChatPage"
import { conversations, currentUser, messagesByConversation } from "../../shared/lib/mock-data"
import type { ChatMessage, Conversation } from "../../shared/lib/types"
import type { HistorySource } from "../messages/history"
import type { CentrifugeFactory } from "../realtime/realtimeClient"

/**
 * Истории задают **данные**, а не состояние соединения.
 *
 * Пять прежних историй (`Connected`, `Reconnecting`, `Disconnected`,
 * `Degraded`, `Syncing`) различали пропс `initialConnectionState`, которого у
 * `ChatPage` больше нет: состояния соединения — предмет
 * `ConnectionStateBanner`, и все пять остались в его собственных историях
 * (`Messages/ConnectionStateBanner`). Здесь же показывается то, что главная
 * панель теперь различает сама: данные ленты и их отсутствие.
 *
 * Соединение в историях задаётся **данными**, а не наблюдением: комната без
 * сети показывает то же, что комната с ней, потому что состояние приходит
 * снаружи и в разметке его никто не выдумывает.
 */

const withoutMessages = conversations.filter((c) => !c.hasMessages)

/** Беседа без `last_message`: превью пустое, и признак это подтверждает. */
const emptyConversation: Conversation = withoutMessages[0] ?? { ...conversations[0], hasMessages: false, lastMessagePreview: "", lastMessageTimestamp: undefined }

/**
 * Молчащая фабрика соединений — шов для историй, а не двойник для проверок.
 *
 * `test-support/centrifuge.ts` здесь не подходит намеренно: тот **наблюдает** —
 * пишет хендлеры и вызовы, чтобы тест мог их прочитать, — и тянуть ради этого
 * записывающее устройство в истории значило бы завести в них наблюдателя,
 * которому нечего наблюдать. Здесь нужен ровно противоположный шов: объект,
 * который принимает вызовы и не делает ничего.
 *
 * `as never` — один раз и по той же причине, что в двойнике: собирать полную
 * поверхность `Centrifuge` руками значило бы обещать интерфейс, которого у
 * истории нет, а частичный объект без приведения не собирается.
 */
const silentConnections: CentrifugeFactory = () =>
  ({
    on: () => {},
    newSubscription: () => ({ on: () => {}, subscribe: () => {} }),
    connect: () => {},
    disconnect: () => {},
  }) as never

/**
 * История из готовых сообщений: снимок отдан целиком и продолжения нет.
 *
 * `hasMore: false` без `nextAfterSeq` — это и есть признак завершения
 * (`sync.ts`): пустой `items` сам по себе сходимостью не является, и история,
 * объявившая её по длине массива, показывала бы пустую беседу несходящейся.
 */
function historyOf(messages: readonly ChatMessage[]): HistorySource {
  const page = { items: messages, hasMore: false, nextAfterSeq: null, syncToSeq: null }

  return {
    loadTail: async () => ({ items: messages, hasMore: false, nextBeforeSeq: null, syncToSeq: null }),
    loadPage: async () => page,
  }
}

/** Снимок не приходит вовсе: лента обязана сказать, что ждёт ответа, а не «пусто». */
const pendingHistory: HistorySource = {
  loadTail: () => new Promise(() => {}),
  loadPage: () => new Promise(() => {}),
}

const meta: Meta<typeof ChatPage> = {
  title: "Conversations/ChatPage",
  component: ChatPage,
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof ChatPage>

const base = {
  conversations,
  currentUser,
  currentUserId: "user-viewer",
  centrifugoUrl: "wss://rt.finops.local/connection/websocket",
  issueTicket: async () => "storybook",
  createCentrifuge: silentConnections,
}

/** Полный список; выбрана первая беседа, её история нарисована. */
export const Default: Story = {
  args: { ...base, history: historyOf(messagesByConversation["anna-petrova"] ?? []) },
}

/** Сервер вернул ноль бесед — это ответ, а не ошибка: шапки нет, список пуст. */
export const NoConversations: Story = {
  args: { ...base, conversations: [], history: historyOf([]) },
}

/** Снимок пуст **и подтверждён**: «No messages yet» — утверждение сервера, а не молчание. */
export const NoMessages: Story = {
  args: { ...base, conversations: [emptyConversation], history: historyOf([]) },
}

/** Снимок ещё в пути: границы нет, и лента говорит о загрузке, а не о пустоте. */
export const HistoryPending: Story = {
  args: { ...base, history: pendingHistory },
}

/** Выбранная беседа с историей из нескольких сообщений. */
export const WithMessages: Story = {
  args: { ...base, conversations: [conversations[0]], history: historyOf(messagesByConversation[conversations[0].id] ?? []) },
}
