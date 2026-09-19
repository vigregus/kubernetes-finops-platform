import { Icon } from "../../../shared/ui/Icon"

interface DeletedMessageNoticeProps {
  timestamp: string
}

export function DeletedMessageNotice({ timestamp }: DeletedMessageNoticeProps) {
  return (
    <div className="my-2 flex justify-start pl-10">
      <div className="inline-flex select-none items-center gap-2 rounded-xl bg-surface-warm-subtle/80 px-3 py-2 text-sm italic text-text-warm-muted">
        <Icon name="delete_outline" size={16} className="opacity-60" />
        <span>Message deleted</span>
        <span className="ml-1 text-[11px] not-italic opacity-80">{timestamp}</span>
      </div>
    </div>
  )
}
