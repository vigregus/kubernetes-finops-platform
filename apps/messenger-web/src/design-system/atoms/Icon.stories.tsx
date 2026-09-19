import type { Meta, StoryObj } from "@storybook/react-vite"
import { Icon } from "./Icon"

const meta: Meta<typeof Icon> = {
  title: "Atoms/Icon",
  component: Icon,
  args: { name: "forum", size: 24 },
}
export default meta
type Story = StoryObj<typeof Icon>

export const Default: Story = {}

export const Gallery: Story = {
  render: () => (
    <div className="flex flex-wrap gap-4 p-6">
      {[
        "forum",
        "search",
        "call",
        "videocam",
        "info",
        "send",
        "attach_file",
        "sentiment_satisfied",
        "done",
        "done_all",
        "schedule",
        "error_outline",
        "refresh",
        "sync",
        "cloud_off",
        "warning",
        "history",
        "block",
        "delete_outline",
        "help_outline",
        "waving_hand",
        "download",
        "mark_email_unread",
        "devices",
        "lock_clock",
        "arrow_forward",
        "image",
        "description",
        "graphic_eq",
        "play_arrow",
        "progress_activity",
      ].map((name) => (
        <div key={name} className="flex w-24 flex-col items-center gap-1 text-center text-xs text-text-warm-muted">
          <Icon name={name} size={22} />
          {name}
        </div>
      ))}
    </div>
  ),
}
