import { Icon } from "../atoms/Icon"

interface EmptyConversationStateProps {
  name: string
}

/** LIST-003: a conversation can exist with zero messages — no crash, no fake preview. */
export function EmptyConversationState({ name }: EmptyConversationStateProps) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface-cream text-text-warm-muted shadow-[0_1px_4px_rgba(41,37,36,0.05)]">
        <Icon name="waving_hand" size={26} />
      </div>
      <p className="text-sm text-text-warm-secondary">No messages yet. Say hello to {name}.</p>
    </div>
  )
}
