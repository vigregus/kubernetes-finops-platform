import { clsx } from "clsx"

import { describeCall, isMissedFor, type CallSummary } from "../../calls/callSummary"
import type { MessageCall } from "../../../shared/lib/types"
import { Icon } from "../../../shared/ui/Icon"

interface CallSummaryNoticeProps {
  call: MessageCall
  isOwn: boolean
  timestamp: string
}

/**
 * Итог звонка в ленте (`CALL-005`): служебная строка по центру, а не пузырь
 * чьей-то реплики — у звонка нет автора в том смысле, в каком он есть у текста.
 * Пропущенный подсвечивается: его читатель должен заметить.
 */
export function CallSummaryNotice({ call, isOwn, timestamp }: CallSummaryNoticeProps) {
  const summary: CallSummary = { outcome: call.outcome, kind: call.kind }
  const missed = isMissedFor(summary, isOwn)
  return (
    <div className="flex justify-center" data-call-summary={call.outcome} data-call-kind={call.kind}>
      <div
        className={clsx(
          "flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-xs font-medium",
          missed
            ? "border-red-200 bg-red-50 text-red-700"
            : "border-outline-variant bg-surface-container-low text-text-warm-secondary",
        )}
      >
        <Icon name={call.kind === "video" ? "videocam" : "call"} size={14} className="flex-shrink-0" />
        <span>{describeCall(summary, isOwn, call.durationSeconds)}</span>
        {timestamp && <span className="text-text-warm-muted">{timestamp}</span>}
      </div>
    </div>
  )
}
