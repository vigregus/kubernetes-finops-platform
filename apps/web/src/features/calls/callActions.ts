/**
 * Действие с уведомления о звонке («Принять», «Отклонить») доезжает до страницы.
 *
 * Два пути, оба приходят от Service Worker (`public/sw.js`): в уже открытую
 * вкладку — сообщением, в новую — адресом `/?call=<id>&action=accept`. Тем же
 * приёмом, что и открытие беседы (`notifications/openConversation.ts`).
 */
export type CallAction = "accept" | "decline" | "open"

export interface PendingCallAction {
  readonly callId: string
  readonly action: CallAction
}

function actionOf(value: unknown): CallAction | null {
  return value === "accept" || value === "decline" || value === "open" ? value : null
}

/** Действие из адреса; всё негодное — `null`. Адрес после этого чистится вызывающим. */
export function callActionFromUrl(search: string): PendingCallAction | null {
  const params = new URLSearchParams(search)
  const callId = params.get("call")
  const action = actionOf(params.get("action"))
  return callId !== null && callId !== "" && action !== null ? { callId, action } : null
}

export function listenForCallAction(onAction: (pending: PendingCallAction) => void): () => void {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return () => {}
  const handler = (event: MessageEvent) => {
    const data: unknown = event.data
    if (typeof data !== "object" || data === null) return
    const record = data as Record<string, unknown>
    const action = actionOf(record["action"])
    if (record["type"] === "call-action" && typeof record["callId"] === "string" && action !== null) {
      onAction({ callId: record["callId"], action })
    }
  }
  navigator.serviceWorker.addEventListener("message", handler)
  // `?.`: к закрытию страницы worker мог исчезнуть вместе со средой.
  return () => navigator.serviceWorker?.removeEventListener("message", handler)
}
