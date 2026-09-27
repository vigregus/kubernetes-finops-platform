/**
 * Квитанция — три чистых правила, и ни одного побочного эффекта.
 *
 * Модуль намеренно без React и без сети: правило «номер идёт только вперёд»
 * должно быть проверяемо там, где нет ни таймеров, ни соединения. Всё, что
 * требует их (дребезг, `visibilitychange`, отправка), живёт в `useReceipts.ts`,
 * а сюда приходят уже готовые числа.
 *
 * Что здесь есть и почему именно так:
 *
 * * `advance` — **приём**. Квитанция от собеседника приходит по best-effort
 *   каналу (`message.read`, D1), и порядок её прихода не гарантирован: старое
 *   устройство доедает свою очередь и присылает прежний номер. Число, двинутое
 *   назад, показало бы прочтение, которого не делали, — поэтому только максимум.
 * * `receiptToSend` — **отправка**. Вкладка не шлёт число ниже уже отправленного
 *   ею же (D6б): повторный запрос с тем же числом — не сдвиг, а отставший —
 *   откат, который сервер удержит `GREATEST`, но который мы не имеем права
 *   отправлять.
 * * `deliveryStateOf` — **отображение**. Состояние своего сообщения выводится из
 *   номеров собеседника, а не из собственных отправок: «доставлено» — факт
 *   собеседника.
 *
 * ## `null` — не ноль, и здесь это разные ветки, а не один случай
 *
 * Квитанции может **не быть вовсе** (собеседник ничего не сообщал) — и это не
 * то же, что «сообщил ноль»: вывести ноль из молчания значило бы утверждать о
 * собеседнике то, чего он не делал. Это то же правило `absence ≠ 0`, что у
 * `unreadCount` и `read_states` в REST, и оно доводится до разметки: состояние
 * `unknown` не рисуется (D4/D12).
 *
 * ## Пара приводится к серверной (`read ≤ delivered`)
 *
 * `read_states` на сервере нормализует пару на входе («прочитать неполученное
 * нельзя»), и клиент, у которого `read > delivered`, показал бы состояние,
 * которого не бывает, и отправил бы такой же запрос. Поэтому прочтение в
 * `advance` поднимает и доставку — это не вывод из молчания, а повтор нормы
 * сервера там, где второго числа в событии нет.
 */

import type { MessageDeliveryState } from "../../shared/lib/types"

/**
 * Два числа квитанции — как их знает одна из сторон.
 *
 * Не `ReadState<T>` из домена сервера и не `ConversationReadStatesInner` из
 * сгенерированного клиента: сюда числа попадают из трёх источников (REST,
 * событие, наблюдатель строк), и общий тип должен принадлежать правилу, а не
 * одному из источников.
 */
export interface Watermarks {
  readonly readSeq: number
  readonly deliveredSeq: number
}

/** Пришедшая квитанция: поля независимы, и хотя бы одно из них есть. */
export interface IncomingWatermarks {
  readonly readSeq?: number
  readonly deliveredSeq?: number
}

/** Что уходит в запрос: только сдвинувшиеся поля. */
export interface OutgoingReceipt {
  readonly readSeq?: number
  readonly deliveredSeq?: number
}

/**
 * Максимум по каждому полю: отставшее событие не двигает ни одно число.
 *
 * `current === null` — «квитанции ещё не было»; это не ноль, и первое известие
 * заводит **оба** числа, а не одно: `Watermarks` без второго числа не бывает,
 * а `read` без `delivered` противоречил бы серверной норме.
 *
 * Пустое известие (ни одного числа) состояния не заводит: «событие без чисел» и
 * «событие» — разные вещи, и разбор (`adaptReadReceipt`) такое уже отбрасывает.
 * Здесь это проверяется ещё раз, потому что сюда числа могут прийти и из REST.
 */
export function advance(
  current: Watermarks | null,
  incoming: IncomingWatermarks,
): Watermarks | null {
  const hasRead = incoming.readSeq !== undefined
  const hasDelivered = incoming.deliveredSeq !== undefined
  if (!hasRead && !hasDelivered) return current

  const previous: Watermarks = current ?? { readSeq: 0, deliveredSeq: 0 }
  const readSeq = Math.max(previous.readSeq, incoming.readSeq ?? 0)
  const deliveredSeq = Math.max(previous.deliveredSeq, incoming.deliveredSeq ?? 0, readSeq)
  return { readSeq, deliveredSeq }
}

/**
 * Что отправить, если отправлять нечего — `null`.
 *
 * «Нечего» — это два разных случая, и они намеренно не различаются: числа не
 * сдвинулись с прошлой отправки **или** присланное отстало от отправленного.
 * Оба дают одно решение — запроса нет.
 */
export function receiptToSend(sent: Watermarks, wanted: Watermarks): OutgoingReceipt | null {
  const readSeq = wanted.readSeq > sent.readSeq ? wanted.readSeq : undefined
  const deliveredSeq =
    wanted.deliveredSeq > sent.deliveredSeq ? wanted.deliveredSeq : undefined
  if (readSeq === undefined && deliveredSeq === undefined) return null
  return {
    ...(readSeq === undefined ? {} : { readSeq }),
    ...(deliveredSeq === undefined ? {} : { deliveredSeq }),
  }
}

/**
 * Состояние своего сообщения по квитанции собеседника.
 *
 * `peer === null` — собеседник ничего не сообщал, и это `sent`, а не
 * `delivered`: доставку из молчания вывести нельзя. `sending`/`retrying`/
 * `failed` недостижимы и здесь: оптимистичной отправки в клиенте нет вовсе
 * (композера нет), и эти состояния не выдумываются.
 */
export function deliveryStateOf(seq: number, peer: Watermarks | null): MessageDeliveryState {
  if (peer === null) return "sent"
  if (peer.readSeq >= seq) return "read"
  if (peer.deliveredSeq >= seq) return "delivered"
  return "sent"
}
