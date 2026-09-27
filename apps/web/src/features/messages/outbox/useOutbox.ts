/**
 * Очередь исходящих — одно место, которое чеканит `client_message_id` и повторяет попытку.
 *
 * Правила очереди (срок, классификация отказа, расписание паузы) живут в
 * `pendingMessages.ts` и проверяются без хранилища. Здесь — только жизненный
 * цикл: восстановление при старте, попытка, таймер, уборка. Разделение не
 * косметическое: единственная причина, по которой правила отделены, — `jsdom`
 * без `IndexedDB`, и она названа в `outboxStore.ts`.
 *
 * ## Идентификатор чеканится при нажатии и живёт в записи
 *
 * `client_message_id` — тождество **логической** отправки, а не попытки. Он
 * создаётся в `enqueue` ровно один раз и уезжает в хранилище вместе с записью;
 * повтор берёт его оттуда. Свежий идентификатор на повторе дал бы на сервере
 * **две** строки — и это единственный исход, отличающий идемпотентный повтор от
 * повторной отправки, поэтому запрет на перечеканку держит `nextAttempt`
 * (её разбор), а не обещание в комментарии.
 *
 * ## Попытка не выносит приговор по одному исходу
 *
 * Исход попытки решает `classify`, а что делать с записью — `nextAttempt`. Здесь
 * нет ни одного `if` про коды: раскладывание статусов по веткам в этом файле
 * завело бы вторую классификацию, расходящуюся с первой молча.
 *
 * `keep` (отсутствие удостоверения) — единственный исход, после которого
 * таймера **не** ставится: очередь ждёт человека, а не времени. Стирание записи
 * на этом исходе запрещено отдельно (D7): написанное — не мусор и не
 * отправленное, и терять его на перелогине нельзя.
 *
 * ## Восстановление: срок выводится заново, а не хранится
 *
 * `replayPlan` применяет `expire` к каждой восстановленной записи, и это
 * идемпотентно: запись, ставшая `failed` по сроку, останется ею и при следующем
 * чтении. Поэтому переписывать её в хранилище не нужно — правило выводит то же
 * самое из `createdAt`. Отсюда же и то, почему `failureReason` не уезжает в
 * `IndexedDB` (перечень в `StoredPendingMessage`): причина — свойство попытки, а
 * истечение выводится.
 *
 * ## Отказ хранилища не отменяет отправку
 *
 * В браузере с выключенным `IndexedDB` (приватное окно, запрет данных сайта)
 * очередь работает **в памяти вкладки**: сообщение уходит, повтор в этой вкладке
 * возможен, перезагрузка его не вернёт. Это названная деградация, а не тишина:
 * `outboxStore` бросает, а здесь отказ виден тем, что запись не сохранилась.
 * Обратная сторона того же — подтверждённое сообщение, чьё удаление из
 * хранилища не прошло: оно вернётся при перезагрузке, но повтор безопасен,
 * потому что `client_message_id` тот же и сервер ответит существующим
 * сообщением (`ensure`-семантика `POST /messages`), а не заведёт второе.
 *
 * ## Запись снимается не ответом, а лентой — и очередь об этом не судит
 *
 * Успешный ответ **не** снимает запись. Ответ — это «сервер принял», а снятие —
 * «подтверждённое видно в ленте», и это разные утверждения: при `gap` ответ
 * доходит, а сообщение в ленту не кладётся (дыра остаётся дырой, `G3-006`), и
 * снятая по ответу запись оставила бы на экране ноль записей на всё время
 * догрузки (D6). Поэтому успешная попытка отдаёт подтверждённое наружу
 * (`onSent`) и **ничего** не решает о записи, а снимает её владелец ленты, когда
 * увидит подтверждённое, — вызовом `settle`.
 *
 * Отсюда же — почему `settle` принимает **идентификаторы**, а не записи: очередь
 * не знает ни про слияние, ни про `client_message_id` внутри сообщения, и
 * вопрос «что снять» решается там, где лежит лента (`eventMerge.confirmedClientIds`).
 * Число мест, знающих про сведение, остаётся одним.
 *
 * Молчание `settle` — не потеря: запись, которую снять не удалось (ответ ушёл,
 * событие ушло, лента не изменилась), просто остаётся и повторяется. Повтор
 * безопасен: идентификатор тот же, сервер ответит существующим сообщением.
 */
import { useCallback, useEffect, useRef, useState } from "react"

import type { Message } from "../../../api/generated"
import type { PendingMessage } from "../../../shared/lib/types"
import {
  classify,
  nextAttempt,
  replayPlan,
  RETRY_POLICY,
  type RetryPolicy,
} from "./pendingMessages"
import type { OutboxStore } from "./outboxStore"

/**
 * Тело отправки: то, что знает очередь, и то, что ждёт транспорт.
 *
 * Собирается здесь, а не отдаётся вызывающим: `clientMessageId` — предмет
 * очереди, и вызывающий, обязанный его помнить, завёл бы второе место, знающее
 * про тождество отправки.
 */
export interface SendMessageRequest {
  readonly conversationId: string
  readonly clientMessageId: string
  readonly text: string
}

export type SendMessage = (request: SendMessageRequest) => Promise<Message>

export interface OutboxOptions {
  readonly store: OutboxStore
  readonly send: SendMessage
  /**
   * Подтверждённое сообщение — наружу, в ленту.
   *
   * Ответ сервера обязан идти **тем же** путём слияния, что история и realtime
   * (D6), и решение об этом принимает владелец ленты, а не очередь: у очереди
   * нет ни границы слияния, ни беседы, в которую пришлось бы применять.
   *
   * Запись при этом **не снимается**: снятие приходит обратно вызовом `settle`,
   * когда подтверждённое окажется в ленте. Снять здесь значило бы при `gap`
   * убрать с экрана обе записи — и оптимистичную, и ещё не догруженную.
   */
  readonly onSent?: (pending: PendingMessage, response: Message) => void
  /** Часы — параметром ради проверяемости срока. */
  readonly now?: () => number
  readonly policy?: RetryPolicy
}

export interface Outbox {
  /** Все записи, по времени написания. Беседы различает вызывающий. */
  readonly pending: readonly PendingMessage[]
  /** Человек нажал Send: запись заводится здесь и здесь же чеканится её тождество. */
  enqueue(conversationId: string, text: string): void
  /**
   * Подтверждённое дошло до ленты — снять эти записи.
   *
   * Идемпотентно: множество, которого в очереди нет, не меняет ни состояние, ни
   * хранилище. Это несущее свойство, а не удобство — сведение зовётся на каждое
   * изменение ленты, и вызов «на всякий случай» обязан быть бесплатным.
   */
  settle(clientMessageIds: Iterable<string>): void
}

export function useOutbox({
  store,
  send,
  onSent,
  now = Date.now,
  policy = RETRY_POLICY,
}: OutboxOptions): Outbox {
  const [pending, setPending] = useState<readonly PendingMessage[]>([])
  const timers = useRef(new Map<string, number>())
  /**
   * Свежая попытка — для таймеров и восстановления.
   *
   * Таймер зовёт функцию, а не замкнутое значение: ссылка на попытку меняется
   * вместе с пропсами, и захваченная копия повторяла бы транспортом, которого
   * уже нет.
   */
  const attemptRef = useRef<(record: PendingMessage) => void>(() => {})

  const upsert = useCallback((record: PendingMessage) => {
    setPending((current) => {
      const rest = current.filter((item) => item.clientMessageId !== record.clientMessageId)
      // Порядок по времени написания, а не по времени прихода в состояние:
      // повторяемая запись обновляется много раз и без сортировки уезжала бы
      // вниз ленты при каждой попытке.
      return [...rest, record].sort((left, right) => left.createdAt - right.createdAt)
    })
  }, [])

  const settle = useCallback(
    (clientMessageIds: Iterable<string>) => {
      const confirmed = new Set(clientMessageIds)
      if (confirmed.size === 0) return

      setPending((current) => {
        const kept = current.filter((item) => !confirmed.has(item.clientMessageId))
        // Ничего не снято — **та же** ссылка. Сведение зовётся на каждое
        // изменение ленты, и новая ссылка на прежнем содержимом гнала бы
        // рендер по кругу: подтверждённых в очереди уже нет, а вызов остался.
        return kept.length === current.length ? current : kept
      })

      // Хранилище чистится по переданному множеству, а не по уцелевшему списку:
      // снимать нечего — значит и там этого идентификатора нет, а отказ
      // хранилища здесь виден тем же, чем и везде, — запись вернётся при
      // перезагрузке, и повтор безопасен (идентификатор тот же).
      for (const clientMessageId of confirmed) {
        void store.remove(clientMessageId).catch(() => {
          // Запись вернётся при перезагрузке, и повтор безопасен: идентификатор
          // тот же, сервер ответит существующим сообщением.
        })
      }
    },
    [store],
  )

  /**
   * Запись в хранилище — и её отказ не повод молчать о попытке.
   *
   * Состояние в памяти уже изменено к этому моменту: показать человеку «идёт
   * повтор» и не завести его в хранилище — деградация, названная в докстринге
   * модуля; не показать вовсе — потеря, которую нечем объяснить.
   */
  const persist = useCallback(
    (record: PendingMessage) => {
      void store.put(record).catch(() => {
        // Отказ хранилища виден тем, что запись не переживёт перезагрузку.
        // Строки на экране для него нет намеренно: сообщение при этом
        // отправляется, и «не удалось сохранить черновик» рядом с уходящим
        // сообщением говорило бы о нашем хранилище там, где исход письма
        // решается транспортом.
      })
    },
    [store],
  )

  const schedule = useCallback((record: PendingMessage, delayMs: number) => {
    const previous = timers.current.get(record.clientMessageId)
    if (previous !== undefined) window.clearTimeout(previous)

    const id = window.setTimeout(() => {
      timers.current.delete(record.clientMessageId)
      attemptRef.current(record)
    }, delayMs)
    timers.current.set(record.clientMessageId, id)
  }, [])

  const attempt = useCallback(
    async (record: PendingMessage) => {
      try {
        const response = await send({
          conversationId: record.conversationId,
          clientMessageId: record.clientMessageId,
          text: record.text,
        })
        // Ответ уходит в ленту — и только туда. Запись остаётся: снимает её
        // `settle`, когда подтверждённое окажется в ленте (докстринг модуля).
        // Ни таймера, ни новой попытки здесь нет — повторять принятое
        // сообщение значило бы повторять то, что уже произошло.
        onSent?.(record, response)
      } catch (error) {
        const outcome = nextAttempt(record, classify(error), now(), policy)
        upsert(outcome.pending)
        persist(outcome.pending)
        if (outcome.delayMs !== null) schedule(outcome.pending, outcome.delayMs)
      }
    },
    [send, upsert, persist, schedule, onSent, now, policy],
  )

  useEffect(() => {
    attemptRef.current = attempt
  })

  const enqueue = useCallback(
    (conversationId: string, text: string) => {
      const record: PendingMessage = {
        clientMessageId: crypto.randomUUID(),
        conversationId,
        text,
        createdAt: now(),
        // `sending` — состояние записи **до** первой попытки; в хранилище оно
        // не уезжает (перечень `StoredPendingMessage` его не знает), потому что
        // положить туда запись успевает только повтор.
        state: "sending",
        attemptCount: 0,
        lastAttemptAt: null,
      }
      upsert(record)
      persist(record)
      void attemptRef.current(record)
    },
    [upsert, persist, now],
  )

  /**
   * Восстановление — один раз на монтирование, а не на каждую смену беседы.
   *
   * Хранилище одно на все беседы, и запись помнит свою: читать его заново при
   * переключении беседы значило бы платить за то, что не менялось, а главное —
   * повторно запускать попытку для всех записей сразу.
   */
  useEffect(() => {
    let cancelled = false

    void (async () => {
      let records: PendingMessage[]
      try {
        records = await store.list()
      } catch {
        // Хранилище недоступно — очередь пуста, и это не «всё отправлено»:
        // отличить одно от другого здесь нечем, и выдумывать исход не будем.
        return
      }
      if (cancelled) return

      const plan = replayPlan(records, now())
      // Показываются все три ведра: просроченное и отвергнутое — утверждения о
      // человеке («это не ушло»), и спрятать их значило бы оставить написанное
      // без следа.
      setPending(
        [...plan.send, ...plan.expired, ...plan.held].sort(
          (left, right) => left.createdAt - right.createdAt,
        ),
      )
      for (const item of plan.send) void attemptRef.current(item)
    })()

    return () => {
      cancelled = true
    }
  }, [store, now])

  useEffect(() => {
    const scheduled = timers.current
    return () => {
      for (const id of scheduled.values()) window.clearTimeout(id)
      scheduled.clear()
    }
  }, [])

  return { pending, enqueue, settle }
}
