/**
 * Очередь, батчинг, выборка и сброс best-effort телеметрии (G3-008).
 *
 * Best-effort означает конкретно это: `record()` никогда не бросает и не
 * возвращает промис, который вызывающему нужно было бы ждать, а `flush()`
 * глотает любой отказ сети/сервера молча. `06-observability.md`,
 * "Телеметрия браузера обязательна": отказ этой точки не должен быть виден
 * пользователю и не влияет на отправку/доставку сообщений.
 */
import type { TelemetryApi } from "../../api/generated"
import type { BrowserTelemetryEvent } from "../../api/generated"
import type { TelemetryEvent, TelemetryEventType } from "./types"

/**
 * Три типа никогда не выбрасываются: ошибка (`js_error`), неудачное
 * восстановление (`recovery_failed`) и сам `delivery_ack` - подрезать
 * выборкой то, из чего строится T_delivery, значило бы занизить гистограмму
 * без единой строки кода, которая это объясняет. Остальные девять -
 * высокочастотные события соединения, где важна доля (`PERF-005`,
 * "доля recovered=false"), а не каждая отдельная запись: равномерная
 * выборка эту долю не искажает.
 */
const ALWAYS_SAMPLED: ReadonlySet<TelemetryEventType> = new Set([
  "delivery_ack",
  "recovery_failed",
  "js_error",
])

const DEFAULT_SAMPLE_RATE = 0.5
// Контрактный потолок пачки (`openapi.yaml`, `BrowserTelemetryBatch.events`,
// maxItems: 50) - то же число, не выбранное заново.
const DEFAULT_MAX_BATCH_SIZE = 50
const DEFAULT_FLUSH_INTERVAL_MS = 10_000

export interface TelemetryClientOptions {
  readonly api: Pick<TelemetryApi, "ingestBrowserTelemetry">
  /** Доля высокочастотных событий, которая реально отправляется. 0..1. */
  readonly sampleRate?: number
  readonly maxBatchSize?: number
  readonly flushIntervalMs?: number
  /** Подменяется в тестах ради детерминированной выборки. */
  readonly random?: () => number
}

export interface TelemetryClient {
  readonly record: (event: TelemetryEvent) => void
  readonly flush: () => Promise<void>
  readonly dispose: () => void
}

function toWire(event: TelemetryEvent): BrowserTelemetryEvent {
  return {
    type: event.type,
    occurredAt: event.occurredAt.toISOString(),
    ...(event.messageId === undefined ? {} : { messageId: event.messageId }),
    ...(event.conversationId === undefined ? {} : { conversationId: event.conversationId }),
    ...(event.detail === undefined ? {} : { detail: event.detail }),
  }
}

export function createTelemetryClient(options: TelemetryClientOptions): TelemetryClient {
  const sampleRate = options.sampleRate ?? DEFAULT_SAMPLE_RATE
  const maxBatchSize = options.maxBatchSize ?? DEFAULT_MAX_BATCH_SIZE
  const random = options.random ?? Math.random

  let queue: TelemetryEvent[] = []
  let disposed = false

  function shouldSample(type: TelemetryEventType): boolean {
    return ALWAYS_SAMPLED.has(type) || random() < sampleRate
  }

  function record(event: TelemetryEvent): void {
    if (disposed || !shouldSample(event.type)) return
    queue.push(event)
    // Потолок пачки - повод сбросить немедленно, а не ждать следующего
    // интервала: иначе часть событий копилась бы до таймера, оставшаяся
    // жизнь очереди была бы молчаливой.
    if (queue.length >= maxBatchSize) void flush()
  }

  async function flush(): Promise<void> {
    if (queue.length === 0) return
    const batch = queue
    queue = []
    try {
      await options.api.ingestBrowserTelemetry({
        browserTelemetryBatch: { events: batch.map(toWire) },
      })
    } catch {
      // Накопленная пачка теряется молча: повтор добавил бы best-effort
      // каналу ту самую задержку/нагрузку, от которой сам факт best-effort
      // и защищает отправку сообщений.
    }
  }

  const interval = setInterval(
    () => void flush(),
    options.flushIntervalMs ?? DEFAULT_FLUSH_INTERVAL_MS,
  )

  // Вкладка, уходящая в фон, - последний надёжный момент сбросить очередь:
  // `pagehide`/`unload` на мобильных браузерах не гарантированы вовсе, а
  // `visibilitychange` в `hidden` происходит раньше и надёжнее них обоих.
  const onVisibilityChange = () => {
    if (document.visibilityState === "hidden") void flush()
  }
  document.addEventListener("visibilitychange", onVisibilityChange)

  function dispose(): void {
    disposed = true
    clearInterval(interval)
    document.removeEventListener("visibilitychange", onVisibilityChange)
  }

  return { record, flush, dispose }
}
