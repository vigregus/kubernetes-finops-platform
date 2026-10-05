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
