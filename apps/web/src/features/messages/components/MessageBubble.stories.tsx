import type { Meta, StoryObj } from "@storybook/react-vite"
import { MessageBubble } from "./MessageBubble"

const meta: Meta<typeof MessageBubble> = {
  title: "Messages/MessageBubble",
  component: MessageBubble,
  parameters: { docs: { description: { component: "States per 07-engineering-standard.md Часть 6: sending -> sent -> delivered -> read, or retrying (offline outbox or a retried attempt) / failed." } } },
  decorators: [(Story) => <div className="max-w-md p-6"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof MessageBubble>

export const IncomingText: Story = {
  args: { message: { id: "1", authorId: "anna", authorName: "Anna Petrova", kind: "text", text: "Hi! Did you see the new revisions?", timestamp: "10:30 AM" } },
}

export const OutgoingSending: Story = {
  args: { message: { id: "2", authorId: "me", kind: "text", text: "Sending this right now...", timestamp: "10:31 AM", deliveryState: "sending" } },
}
export const OutgoingSent: Story = {
  args: { message: { id: "3", authorId: "me", kind: "text", text: "Committed on the server.", timestamp: "10:31 AM", deliveryState: "sent" } },
}
export const OutgoingDelivered: Story = {
  args: { message: { id: "4", authorId: "me", kind: "text", text: "Reached their device.", timestamp: "10:31 AM", deliveryState: "delivered" } },
}
export const OutgoingRead: Story = {
  args: { message: { id: "5", authorId: "me", kind: "text", text: "They've read this.", timestamp: "10:31 AM", deliveryState: "read" } },
}
export const OutgoingRetrying: Story = {
  name: "Outgoing — retrying (offline outbox)",
  args: { message: { id: "6", authorId: "me", kind: "text", text: "Queued until we're back online.", timestamp: "10:31 AM", deliveryState: "retrying" } },
}
export const OutgoingFailed: Story = {
  args: { message: { id: "7", authorId: "me", kind: "text", text: "This didn't make it.", timestamp: "10:31 AM", deliveryState: "failed" } },
}
export const OutgoingFailedBlocked: Story = {
  name: "Outgoing — failed (CLI-004 specific reason)",
  args: {
    message: { id: "8", authorId: "me", kind: "text", text: "Sent while offline, rejected on replay.", timestamp: "10:31 AM", deliveryState: "failed", failureReason: "Not delivered — you were blocked" },
  },
}
