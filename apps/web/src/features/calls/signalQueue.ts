/**
 * Очередь отправки сигналов: **строго по одному**.
 *
 * `offer` и пачка кандидатов, отправленные параллельно, достигают сервера в любом
 * порядке, а номер (`seq`) выдаётся по порядку прихода — и `offer` получал бы
 * номер больше кандидатов, пришедших раньше него. Получатель отбрасывает всё
 * «не новее» виденного, и `offer` пропадал бы, а звонок не устанавливался.
 *
 * Сбой одного сигнала не останавливает очередь: он возвращается вызывающему, а
 * следующие идут своим чередом.
 */
export function createSerialQueue<T>(run: (item: T) => Promise<void>): (item: T) => Promise<void> {
  let tail: Promise<unknown> = Promise.resolve()
  return (item) => {
    const result = tail.then(() => run(item))
    tail = result.catch(() => undefined)
    return result
  }
}


/** Повторять ли после этой ошибки: сеть и 5xx — да, отказ клиента (4xx) — нет. */
export type Retryable = (error: unknown) => boolean

export interface RetryOptions {
  /** Паузы перед повторами; число повторов — их количество. */
  readonly delaysMs?: readonly number[]
  readonly retryable?: Retryable
  readonly sleep?: (ms: number) => Promise<void>
}

/**
 * Ограниченный повтор отправки сигнала. Сервер отвечает `503`, когда не смог
 * **передать** сигнал собеседнику (канал звонков без истории): повтор с тем же
 * `signal_id` безопасен, потому что получатель отбрасывает дубль.
 *
 * Не бесконечный: звонок, которому сигнал не удалось доставить за несколько
 * секунд, всё равно не установится, и об этом должен узнать вызывающий.
 */
export async function withRetry(
  run: () => Promise<void>,
  options: RetryOptions = {},
): Promise<void> {
  const delays = options.delaysMs ?? [400, 1200]
  const retryable = options.retryable ?? (() => true)
  const sleep = options.sleep ?? ((ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)))
  let attempt = 0
  for (;;) {
    try {
      await run()
      return
    } catch (error) {
      const delay = delays[attempt]
      if (delay === undefined || !retryable(error)) throw error
      attempt += 1
      await sleep(delay)
    }
  }
}
