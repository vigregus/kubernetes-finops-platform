import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { createTelemetryClient, type TelemetryClientOptions } from "./telemetryClient"
import type { TelemetryEvent } from "./types"

function событие(type: TelemetryEvent["type"], extra: Partial<TelemetryEvent> = {}): TelemetryEvent {
  return { type, occurredAt: new Date("2026-09-19T00:00:00Z"), ...extra }
}

function givenApi() {
  const batches: unknown[] = []
  const ingestBrowserTelemetry = vi.fn(async (params: { browserTelemetryBatch: { events: unknown[] } }) => {
    batches.push(params.browserTelemetryBatch.events)
  })
  return { api: { ingestBrowserTelemetry }, batches }
}

function givenClient(overrides: Partial<TelemetryClientOptions> = {}) {
  const { api, batches } = givenApi()
  const client = createTelemetryClient({ api, random: () => 0, ...overrides })
  return { client, api, batches }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe("createTelemetryClient", () => {
  it("не отправляет ничего, пока очередь пуста", async () => {
    const { client, api } = givenClient()
    await client.flush()
    expect(api.ingestBrowserTelemetry).not.toHaveBeenCalled()
    client.dispose()
  })

  it("копит события и отправляет их по flush() одной пачкой", async () => {
    const { client, batches } = givenClient()
    client.record(событие("ws_connected"))
    client.record(событие("ws_disconnected"))
    await client.flush()

    expect(batches).toHaveLength(1)
    expect(batches[0]).toEqual([
      { type: "ws_connected", occurredAt: "2026-09-19T00:00:00.000Z" },
      { type: "ws_disconnected", occurredAt: "2026-09-19T00:00:00.000Z" },
    ])
    client.dispose()
  })

  it("переводит доменное событие в транспортную форму, пропуская необязательные поля", async () => {
    const { client, batches } = givenClient()
    client.record(событие("delivery_ack", { messageId: "м-1", conversationId: "б-1" }))
    await client.flush()

    expect(batches[0]).toEqual([
      {
        type: "delivery_ack",
        occurredAt: "2026-09-19T00:00:00.000Z",
        messageId: "м-1",
        conversationId: "б-1",
      },
    ])
    client.dispose()
  })

  it("сбрасывает очередь по таймеру, без явного flush()", async () => {
    const { client, batches } = givenClient({ flushIntervalMs: 1000 })
    client.record(событие("ws_connected"))

    await vi.advanceTimersByTimeAsync(1000)

    expect(batches).toHaveLength(1)
    client.dispose()
  })

  it("сбрасывает очередь немедленно по достижении потолка пачки", async () => {
    const { client, batches } = givenClient({ maxBatchSize: 2, flushIntervalMs: 100_000 })
    client.record(событие("ws_connected"))
    client.record(событие("ws_disconnected"))
    // Вторая запись сама вызвала flush синхронно (void flush()) - микротаска
    // ещё не разрешилась, и даём ей шанс.
    await Promise.resolve()
    await Promise.resolve()

    expect(batches).toHaveLength(1)
    client.dispose()
  })

  it("теряет пачку молча при отказе сети - best-effort, не повторяет", async () => {
    const { api } = givenApi()
    api.ingestBrowserTelemetry.mockRejectedValueOnce(new Error("сеть недоступна"))
    const client = createTelemetryClient({ api, random: () => 0 })

    client.record(событие("ws_connected"))
    await expect(client.flush()).resolves.toBeUndefined()

    client.dispose()
  })

  it("delivery_ack/recovery_failed/js_error отправляются всегда, независимо от выборки", async () => {
    const { client, batches } = givenClient({ sampleRate: 0, random: () => 0.999 })
    client.record(событие("delivery_ack", { messageId: "м-1" }))
    client.record(событие("recovery_failed"))
    client.record(событие("js_error", { detail: "TypeError" }))
    // Высокочастотное событие при той же выборке (0.999 >= sampleRate=0)
    // отбрасывается - разница и есть предмет проверки.
    client.record(событие("ws_connected"))

    await client.flush()

    const types = (batches[0] as Array<{ type: string }>).map((e) => e.type)
    expect(types).toEqual(["delivery_ack", "recovery_failed", "js_error"])
    client.dispose()
  })

  it("высокочастотное событие проходит выборку, когда random() ниже sampleRate", async () => {
    const { client, batches } = givenClient({ sampleRate: 0.5, random: () => 0.1 })
    client.record(событие("message_received", { messageId: "м-1" }))
    await client.flush()

    expect(batches).toHaveLength(1)
    client.dispose()
  })

  it("сбрасывает очередь, когда вкладка уходит в фон", async () => {
    const { client, batches } = givenClient()
    client.record(событие("ws_connected"))

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    })
    document.dispatchEvent(new Event("visibilitychange"))
    await Promise.resolve()
    await Promise.resolve()

    expect(batches).toHaveLength(1)
    client.dispose()
  })

  it("dispose() останавливает таймер и перестаёт принимать события", async () => {
    const { client, batches } = givenClient({ flushIntervalMs: 1000 })
    client.dispose()
    client.record(событие("ws_connected"))

    await vi.advanceTimersByTimeAsync(2000)

    expect(batches).toHaveLength(0)
  })
})
