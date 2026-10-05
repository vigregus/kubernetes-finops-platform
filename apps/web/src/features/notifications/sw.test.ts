import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it, vi } from "vitest"

/**
 * Service Worker — обычный файл в `public/`, без сборки, поэтому проверяется
 * как есть: исполняется в песочнице с подменённым `self`, и события ему
 * подаются вручную. Так видно, что он делает на `push` и на нажатие, без
 * браузера и без настоящей подписки.
 */
const SOURCE = readFileSync(join(__dirname, "../../../public/sw.js"), "utf-8")

type Listener = (event: unknown) => void

function loadWorker() {
  const listeners = new Map<string, Listener>()
  const shown: { title: string; options: Record<string, unknown> }[] = []
  const windows: { focus: ReturnType<typeof vi.fn>; postMessage: ReturnType<typeof vi.fn> }[] = []
  const opened: string[] = []
  const self = {
    addEventListener: (type: string, listener: Listener) => listeners.set(type, listener),
    skipWaiting: vi.fn(),
    clients: {
      claim: vi.fn(async () => {}),
      matchAll: vi.fn(async () => windows),
      openWindow: vi.fn(async (url: string) => {
        opened.push(url)
      }),
    },
    registration: {
      showNotification: vi.fn(async (title: string, options: Record<string, unknown>) => {
        shown.push({ title, options })
      }),
    },
  }
  new Function("self", SOURCE)(self)

  const run = async (type: string, event: Record<string, unknown>) => {
    let pending: Promise<unknown> = Promise.resolve()
    listeners.get(type)?.({ ...event, waitUntil: (p: Promise<unknown>) => (pending = p) })
    await pending
  }
  return { self, shown, windows, opened, run, listeners }
}

const push = (data: unknown) => ({ data: { json: () => data } })

describe("Service Worker уведомлений", () => {
  it("слушает ровно push, нажатие и жизненный цикл — fetch не перехватывает", () => {
    const { listeners } = loadWorker()
    expect([...listeners.keys()].sort()).toEqual(["activate", "install", "notificationclick", "push"])
  })

  it("обновляется немедленно и берёт управление: кэшировать нечему", async () => {
    const { self, run } = loadWorker()
    await run("install", {})
    await run("activate", {})
    expect(self.skipWaiting).toHaveBeenCalled()
    expect(self.clients.claim).toHaveBeenCalled()
  })

  it("на push показывает сигнал без текста, тегом беседы (NTF-001, NTF-009)", async () => {
    const { shown, run } = loadWorker()
    await run("push", push({ type: "message", conversation_id: "c-1" }))
    expect(shown).toHaveLength(1)
    expect(shown[0].title).toBe("New message")
    expect(shown[0].options.tag).toBe("conversation:c-1")
    expect(shown[0].options.data).toEqual({ conversationId: "c-1" })
    // В уведомлении нет ни текста сообщения, ни имени отправителя.
    expect(JSON.stringify(shown[0].options)).not.toMatch(/sender|text/i)
  })

  it("негодное тело всё равно даёт уведомление: browser требует показывать на каждый push", async () => {
    const { shown, run } = loadWorker()
    await run("push", { data: { json: () => { throw new Error("не json") } } })
    await run("push", { data: null })
    expect(shown).toHaveLength(2)
    expect(shown[0].options.tag).toBe("message")
  })

  it("нажатие открывает беседу в уже открытой вкладке", async () => {
    const { windows, opened, run } = loadWorker()
    const tab = { focus: vi.fn(async () => {}), postMessage: vi.fn() }
    windows.push(tab)
    const close = vi.fn()
    await run("notificationclick", { notification: { close, data: { conversationId: "c-1" } } })
    expect(close).toHaveBeenCalled()
    expect(tab.focus).toHaveBeenCalled()
    expect(tab.postMessage).toHaveBeenCalledWith({ type: "open-conversation", conversationId: "c-1" })
    expect(opened).toEqual([])
  })

  it("без открытой вкладки нажатие открывает приложение на нужной беседе (NTF-008)", async () => {
    const { opened, run } = loadWorker()
    await run("notificationclick", {
      notification: { close: vi.fn(), data: { conversationId: "c 1/2" } },
    })
    expect(opened).toEqual(["/?conversation=c%201%2F2"])
  })

  describe("уведомление о звонке", () => {
    const click = (action: string) => ({
      action,
      notification: { close: vi.fn(), data: { callId: "call-1" } },
    })

    it("«Принять» в открытой вкладке: вкладка выходит вперёд и получает действие", async () => {
      const { windows, run, opened } = loadWorker()
      const tab = { focus: vi.fn(async () => {}), postMessage: vi.fn() }
      windows.push(tab)
      await run("notificationclick", click("accept"))
      expect(tab.focus).toHaveBeenCalled()
      expect(tab.postMessage).toHaveBeenCalledWith({ type: "call-action", action: "accept", callId: "call-1" })
      expect(opened).toEqual([])
    })

    it("«Отклонить» передаётся как есть", async () => {
      const { windows, run } = loadWorker()
      const tab = { focus: vi.fn(async () => {}), postMessage: vi.fn() }
      windows.push(tab)
      await run("notificationclick", click("decline"))
      expect(tab.postMessage).toHaveBeenCalledWith({ type: "call-action", action: "decline", callId: "call-1" })
    })

    it("нажатие на само уведомление (без кнопки) — просто открыть, а не принять", async () => {
      const { windows, run } = loadWorker()
      const tab = { focus: vi.fn(async () => {}), postMessage: vi.fn() }
      windows.push(tab)
      await run("notificationclick", click(""))
      expect(tab.postMessage).toHaveBeenCalledWith({ type: "call-action", action: "open", callId: "call-1" })
    })

    it("вкладок нет — приложение открывается с действием в адресе", async () => {
      const { run, opened } = loadWorker()
      await run("notificationclick", click("accept"))
      expect(opened).toEqual(["/?call=call-1&action=accept"])
    })

    it("неизвестное действие не становится принятием", async () => {
      const { run, opened } = loadWorker()
      await run("notificationclick", click("format-disk"))
      expect(opened).toEqual(["/?call=call-1&action=open"])
    })

    it("уведомление о сообщении ведёт себя как прежде", async () => {
      const { run, opened } = loadWorker()
      await run("notificationclick", {
        action: "",
        notification: { close: vi.fn(), data: { conversationId: "c-9" } },
      })
      expect(opened).toEqual(["/?conversation=c-9"])
    })
  })
})
