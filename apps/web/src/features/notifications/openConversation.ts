/**
 * Нажатие на уведомление открывает нужную беседу (`NTF-008`).
 *
 * Два пути, и оба приходят от Service Worker: новая вкладка открывается на
 * `/?conversation=<id>`, а в уже открытую worker шлёт сообщение.
 */
export function conversationFromUrl(search: string): string | null {
  const id = new URLSearchParams(search).get("conversation")
  return id !== null && id !== "" ? id : null
}

export function listenForOpenConversation(onOpen: (conversationId: string) => void): () => void {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return () => {}
  const handler = (event: MessageEvent) => {
    const data: unknown = event.data
    if (typeof data !== "object" || data === null) return
    const record = data as Record<string, unknown>
    if (record["type"] === "open-conversation" && typeof record["conversationId"] === "string") {
      onOpen(record["conversationId"])
    }
  }
  navigator.serviceWorker.addEventListener("message", handler)
  return () => navigator.serviceWorker.removeEventListener("message", handler)
}
