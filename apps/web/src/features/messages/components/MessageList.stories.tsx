import type { Meta, StoryObj } from "@storybook/react-vite"
import { MessageList } from "./MessageList"
import { conversations, messagesByConversation } from "../../../shared/lib/mock-data"

/**
 * Истории различают не оформление, а **границу** — то единственное поле, из
 * которого приёмка читает сходимость.
 *
 * Три истории стоят ровно на трёх состояниях границы, и все три различимы в
 * разметке: неизвестна (`data-applied-through-seq` отсутствует), равна нулю
 * (атрибут есть) и непустая. Показать здесь «пустую ленту» без различения
 * нуля и неизвестного значило бы сделать вид, что это одно и то же, — а
 * именно на этом различии и держится пустая беседа (B23).
 *
 * Лента здесь не сортирует ничего и не дедуплицирует: ей это запрещено (B22),
 * порядок приходит из `eventMerge`. Поэтому данные берутся готовыми, в том
 * виде, в каком их отдал бы редьюсер.
 */

const WITH_MESSAGES = messagesByConversation[conversations[0].id] ?? []
const HEAD = WITH_MESSAGES.at(-1)?.seq ?? 0

const meta: Meta<typeof MessageList> = {
  title: "Messages/MessageList",
  component: MessageList,
  parameters: { layout: "fullscreen" },
}
export default meta
type Story = StoryObj<typeof MessageList>

/** Снимка ещё не было: граница неизвестна, и атрибута в разметке нет. */
export const BoundaryUnknown: Story = {
  args: { messages: [], conversationName: conversations[0].name, appliedThroughSeq: null },
}

/** Снимок пришёл пустым: это успех с границей `0`, а не отсутствие ответа. */
export const BoundaryZero: Story = {
  args: { messages: [], conversationName: conversations[0].name, appliedThroughSeq: 0 },
}

/** Снимок применён: граница — голова страницы, и лента её показывает. */
export const WithMessages: Story = {
  args: {
    messages: WITH_MESSAGES,
    conversationName: conversations[0].name,
    appliedThroughSeq: HEAD,
  },
}
