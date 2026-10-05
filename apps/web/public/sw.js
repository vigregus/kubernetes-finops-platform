/*
 * Service Worker — только Web Push. Ничего не кэширует и не перехватывает
 * запросы: обработчика `fetch` здесь нет намеренно.
 *
 * Это не лень, а граница риска (`docs/messenger/13-client-compatibility.md`,
 * часть 1): Service Worker, кэширующий страницу, способен навсегда помешать
 * собственному исправлению — выкатка недоступна уже пострадавшим. Worker без
 * кэша и без `fetch` такой беды причинить не может, поэтому и обновляется
 * немедленно (`skipWaiting`): смешения версий в одном браузере не возникает —
 * кэшировать нечему, а поведение у версий одно и то же: показать сигнал.
 *
 * Уведомление несёт **сигнал, а не текст** (`11-threat-model.md`, Р-5): какое
 * сообщение пришло, человек читает в приложении. Поэтому здесь нечего
 * показывать об удалённом сообщении (`NTF-009`): текста в уведомлении нет.
 *
 * Файл отдаётся с `Cache-Control: no-cache` (`nginx.conf`): иначе он не смог бы
 * обновить сам себя.
 */
self.addEventListener("install", () => {
  self.skipWaiting()
})

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim())
})

function conversationOf(event) {
  try {
    const data = event.data ? event.data.json() : null
    if (data && typeof data.conversation_id === "string") return data.conversation_id
  } catch {
    // Негодное тело — всё равно уведомление: `userVisibleOnly` обязывает
    // показывать что-то на каждый push, иначе браузер покажет своё.
  }
  return null
}

self.addEventListener("push", (event) => {
  const conversationId = conversationOf(event)
  event.waitUntil(
    self.registration.showNotification("New message", {
      body: "Open Vector to read it.",
      // Один тег на беседу: пять сообщений подряд — одно уведомление, и
      // повторная доставка того же сигнала его не дублирует.
      tag: conversationId ? `conversation:${conversationId}` : "message",
      data: { conversationId },
      icon: "/favicon.svg",
    }),
  )
})

self.addEventListener("notificationclick", (event) => {
  event.notification.close()
  const data = event.notification.data || {}

  // Уведомление о звонке: нажатие и кнопки «Принять»/«Отклонить» ведут в приложение
  // и несут действие. Открытая вкладка получает его сообщением, новая — в адресе.
  if (typeof data.callId === "string") {
    const action = event.action === "accept" || event.action === "decline" ? event.action : "open"
    event.waitUntil(
      (async () => {
        const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true })
        const existing = windows[0]
        if (existing) {
          await existing.focus()
          existing.postMessage({ type: "call-action", action, callId: data.callId })
          return
        }
        await self.clients.openWindow(
          `/?call=${encodeURIComponent(data.callId)}&action=${action}`,
        )
      })(),
    )
    return
  }

  const conversationId = data.conversationId || null
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true })
      const existing = windows[0]
      if (existing) {
        // Открытая вкладка получает беседу сообщением и выходит вперёд:
        // вторая вкладка с тем же приложением не нужна.
        await existing.focus()
        existing.postMessage({ type: "open-conversation", conversationId })
        return
      }
      const target = conversationId ? `/?conversation=${encodeURIComponent(conversationId)}` : "/"
      await self.clients.openWindow(target)
    })(),
  )
})
