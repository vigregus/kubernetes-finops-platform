/**
 * Итог звонка в ленте. Сервер кладёт в системное сообщение **код**
 * (`call.completed.audio`, `domain/call.py::summary_code`), а не фразу: на
 * сервере нет языка интерфейса, и «Пропущенный звонок» обязан читаться на языке
 * читателя. Слова подставляет клиент — здесь.
 */
import type { CallKind } from "./callState"

export type SummaryOutcome =
  | "completed"
  | "declined"
  | "missed"
  | "cancelled"
  | "busy"
  | "unavailable"
  | "failed"

export interface CallSummary {
  readonly outcome: SummaryOutcome
  readonly kind: CallKind
}

const OUTCOMES: readonly string[] = [
  "completed",
  "declined",
  "missed",
  "cancelled",
  "busy",
  "unavailable",
  "failed",
]

/** Код из текста системного сообщения; всё остальное — не звонок. */
export function parseCallSummary(text: string | undefined | null): CallSummary | null {
  if (typeof text !== "string") return null
  const match = /^call\.([a-z]+)\.(audio|video)$/.exec(text)
  if (match === null) return null
  const outcome = match[1] ?? ""
  if (!OUTCOMES.includes(outcome)) return null
  return { outcome: outcome as SummaryOutcome, kind: match[2] as CallKind }
}

/** `1:05` / `12:34` / `1:02:03` — длительность без лишних нулей спереди. */
export function formatCallDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const ss = String(s).padStart(2, "0")
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`
}

/**
 * Подпись итога. `own` — звонок мой (я звонящий): «Missed call» у звонящего
 * читается как «Not answered», а не как пропущенный им самим.
 */
export function describeCall(
  summary: CallSummary,
  own: boolean,
  durationSeconds?: number,
): string {
  const noun = summary.kind === "video" ? "Video call" : "Voice call"
  switch (summary.outcome) {
    case "completed":
      return durationSeconds !== undefined ? `${noun}, ${formatCallDuration(durationSeconds)}` : noun
    case "missed":
      return own ? `${noun} · No answer` : `Missed ${noun.toLowerCase()}`
    case "declined":
      return own ? `${noun} · Declined` : `${noun} · You declined`
    case "cancelled":
      return own ? `${noun} · Cancelled` : `Missed ${noun.toLowerCase()}`
    case "busy":
      return own ? `${noun} · Busy` : `Missed ${noun.toLowerCase()}`
    case "unavailable":
      return own ? `${noun} · Unavailable` : `Missed ${noun.toLowerCase()}`
    case "failed":
      return `${noun} · Connection failed`
  }
}

/** Попало ли в «пропущенные» у читателя — подсветка красным. */
export function isMissedFor(summary: CallSummary, own: boolean): boolean {
  if (summary.outcome === "failed") return true
  if (own) return false
  return ["missed", "cancelled", "busy", "unavailable"].includes(summary.outcome)
}

/** Превью в списке бесед: коротко. */
export function callPreview(summary: CallSummary, own: boolean): string {
  if (summary.outcome === "completed") return summary.kind === "video" ? "Video call" : "Voice call"
  return isMissedFor(summary, own) ? "Missed call" : "Call"
}
