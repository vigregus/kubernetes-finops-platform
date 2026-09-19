import { useState, type KeyboardEvent } from "react"
import { Icon } from "../atoms/Icon"
import { IconButton } from "../atoms/IconButton"
import { TextField } from "../atoms/TextField"

interface MessageComposerProps {
  recipientName: string
  onSend: (text: string) => void
  disabled?: boolean
  disabledReason?: string
}

export function MessageComposer({ recipientName, onSend, disabled, disabledReason }: MessageComposerProps) {
  const [value, setValue] = useState("")

  function submit() {
    const text = value.trim()
    if (!text || disabled) return
    onSend(text)
    setValue("")
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  if (disabled) {
    return (
      <div className="flex-shrink-0 bg-surface/90 p-4 shadow-[0_-2px_12px_rgba(41,37,36,0.03)] backdrop-blur-md">
        <div className="mx-auto flex max-w-4xl items-center justify-center gap-2 rounded-2xl bg-surface-container-low p-3.5 text-sm text-text-warm-muted">
          <Icon name="block" size={16} />
          {disabledReason ?? "You can't send messages here."}
        </div>
      </div>
    )
  }

  return (
    <div className="flex-shrink-0 bg-surface/90 p-4 shadow-[0_-2px_12px_rgba(41,37,36,0.03)] backdrop-blur-md">
      <div className="mx-auto flex max-w-4xl flex-col gap-2">
        <div className="flex items-center gap-2 rounded-2xl bg-surface-cream p-2 shadow-[0_2px_8px_rgba(41,37,36,0.05)] transition-all focus-within:shadow-[0_0_0_2px_rgba(234,88,12,0.25)]">
          <IconButton icon="attach_file" label="Attach file" />
          <TextField
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Message ${recipientName}...`}
            className="px-1 py-2 text-sm text-text-charcoal"
          />
          <IconButton icon="sentiment_satisfied" label="Add emoji" />
          <button
            type="button"
            onClick={submit}
            className="flex h-10 flex-shrink-0 items-center justify-center gap-1.5 rounded-xl bg-accent-terracotta px-4 text-sm font-semibold text-on-primary shadow-[0_2px_6px_rgba(234,88,12,0.25)] transition-all hover:bg-status-error active:scale-95"
          >
            Send
            <Icon name="send" size={18} />
          </button>
        </div>
        <div className="flex items-center justify-between px-2 text-xs text-text-warm-muted">
          <span>Enter to send, Shift + Enter for new line</span>
          <span className="flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full bg-status-success" />
            All messages protected
          </span>
        </div>
      </div>
    </div>
  )
}
