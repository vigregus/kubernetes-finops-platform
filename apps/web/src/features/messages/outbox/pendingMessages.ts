/**
 * Правила очереди офлайна — чистые функции, и это не стилистическая мелочь.
 *
 * Очередь придётся восстановить после перезагрузки (и после падения браузера),
 * а её решения — «повторять или сказать человеку, что не вышло», «не истёк ли
 * срок», «растить паузу или взять серверную» — не должны зависеть ни от
 * `IndexedDB`, которого в `jsdom` нет, ни от часов, которые в проверке надо
 * подставить. Поэтому часы и политика повтора приходят **параметрами**, а
 * хранилище и таймеры живут отдельно (`outboxStore.ts`, `useOutbox.ts`).
 *
 * Что здесь решается и почему именно так — в докстринге `classify`.
 */

import {
  ApiProblem,
  UnauthenticatedError,
  SessionExpiredError,
} from "../../../api/problems";
import type { PendingMessage } from "../../../shared/lib/types";

/**
 * Семь суток — срок, после которого запись перестаёт быть отправляемой.
 *
 * Срок проверяется **условием**, а не таймером: таймер, поставленный на неделю,
 * означал бы отправку через семь дней после закрытия вкладки — то есть сообщение
 * из прошлого, которого человек давно не ждёт, уехало бы адресату. Условие
 * проверяется в двух точках — при восстановлении и перед каждой попыткой, —
 * и в обеих просроченная запись становится `failed`, а не отправляется.
 */
export const OUTBOX_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * Своя политика паузы, когда сервер не сказал, когда повторять.
 *
 * Рост вдвое от `baseMs` и потолок `maxMs`: без потолка двадцатая попытка
 * ждала бы одиннадцать суток — дольше самого срока записи, то есть повтор
 * не наступил бы никогда, а очередь выглядела бы живой.
 */
export const RETRY_POLICY = { baseMs: 1000, maxMs: 30_000 } as const;

export interface RetryPolicy {
  readonly baseMs: number;
  readonly maxMs: number;
}

/**
 * Что отказ делает с записью — три исхода, и третьего класса без него не
 * выразить.
 *
 * `retry` — сервер не отказал по существу: сеть не ответила, он сломался,
 * попросил подождать. Повтор при восстановлении связи.
 *
 * `final` — сервер ответил определённо. Повторять нечего: условия, при которых
 * отказ пройдёт, наступят не от нашего повтора, и очередь, повторяющая `403`,
 * крутится вечно. Человеку — надпись и причина.
 *
 * `keep` — `401`. Это **не** отказ отправки и **не** повод стирать: человек
 * ушёл на вход и вернётся, а написанное — единственная его копия. Стирание
 * очереди на `401` означало бы, что текст пропадает на перелогине, и заметить
 * это человек смог бы только по пропаже.
 */
export type Verdict =
  | { readonly kind: "retry"; readonly retryAfterMs: number | null }
  | { readonly kind: "final"; readonly reason: string }
  | { readonly kind: "keep" };

/**
 * Статусы, на которых повтор бессмысленен.
 *
 * `400`/`422` — тело отвергнуто схемой, `403` — не имеем права (заблокирован,
 * адрес не подтверждён), `404` — беседы нет. Все четыре означают одно: сервер
 * рассмотрел запрос и сказал «нет».
 */
const FINAL_STATUSES: ReadonlySet<number> = new Set([400, 403, 404, 422]);

/** Причина, когда сервер отказал, но `code` не назвал. */
const UNNAMED_REJECTION = "rejected";

export function classify(error: unknown): Verdict {
  // Сессия — раньше всего остального, и по имени класса, а не по статусу:
  // `401` приходит двумя разными типами (`client.check` разбирает его в
  // `SessionExpiredError`/`UnauthenticatedError`), и оба означают одно и то же
  // для записи — она остаётся.
  if (error instanceof SessionExpiredError || error instanceof UnauthenticatedError) {
    return { kind: "keep" };
  }
  if (error instanceof ApiProblem) {
    // Защитный вход того же класса: `401`, не разобранный вызывающим.
    if (error.status === 401) {
      return { kind: "keep" };
    }
    if (error.status === 429) {
      return {
        kind: "retry",
        // `undefined` — «сервер не сказал»; `null` в вердикте значит ровно это,
        // и превращать его в ноль нельзя: ноль — приглашение повторить сейчас.
        retryAfterMs: error.retryAfterSeconds === undefined ? null : error.retryAfterSeconds * 1000,
      };
    }
    if (FINAL_STATUSES.has(error.status)) {
      return { kind: "final", reason: error.code ?? UNNAMED_REJECTION };
    }
    // Остаток `ApiProblem` — `5xx`: сервер сломался, а не отказал.
    return { kind: "retry", retryAfterMs: null };
  }
  // Сеть не ответила вовсе (`ServiceUnavailableError` из `client.send`), сбой
  // в нашем коде, что угодно ещё. Неизвестный отказ **не** свидетельство
  // определённого отказа: выбросить запись даёт право слово сервера, а не наша
  // ошибка. Цена ошибки в эту сторону — запись, повисев до срока, станет
  // `failed`, и это видно; цена в обратную — потерянный текст.
  return { kind: "retry", retryAfterMs: null };
}

export function isExpired(pending: PendingMessage, now: number): boolean {
  return now - pending.createdAt >= OUTBOX_TTL_MS;
}

/**
 * Применяет срок: просроченная запись становится `failed`.
 *
 * Для непросроченной возвращается **та же ссылка**, а не копия: правило,
 * пересобирающее запись на каждом чтении, стирало бы поле, которого оно не
 * знает, и следующее добавленное поле пропадало бы молча.
 *
 * Уже отвергнутая сервером не переписывается: у неё есть причина, и подменять
 * её на «expired» значило бы объявить истёкшим то, что сервер отверг по существу.
 */
export function expire(pending: PendingMessage, now: number): PendingMessage {
  if (pending.state === "failed" || !isExpired(pending, now)) {
    return pending;
  }
  return { ...pending, state: "failed", failureReason: "expired" };
}

export interface AttemptOutcome {
  readonly pending: PendingMessage;
  /** Через сколько пробовать снова; `null` — повтора не будет. */
  readonly delayMs: number | null;
}

/**
 * Результат попытки: чем стала запись и когда пробовать снова.
 *
 * `clientMessageId` **не перечеканивается**: с новым идентификатором сервер
 * завёл бы второе сообщение, и в ленте оказались бы обе копии — то, ради чего
 * идемпотентность по `client_message_id` и заведена. Это единственное, что
 * отличает повтор от повторной отправки.
 *
 * При `keep` возвращается та же ссылка и `null` в паузе: попытка была, исхода
 * у неё нет, очередь ждёт человека, а не времени. Счётчик попыток здесь не
 * двигается — иначе очередь считала бы попытки, которых не делала.
 */
export function nextAttempt(
  pending: PendingMessage,
  verdict: Verdict,
  now: number,
  policy: RetryPolicy = RETRY_POLICY,
): AttemptOutcome {
  switch (verdict.kind) {
    case "keep":
      return { pending, delayMs: null };
    case "final":
      return {
        pending: { ...pending, state: "failed", failureReason: verdict.reason },
        delayMs: null,
      };
    case "retry": {
      const own = Math.min(policy.baseMs * 2 ** pending.attemptCount, policy.maxMs);
      const delayMs = verdict.retryAfterMs === null ? own : Math.max(own, verdict.retryAfterMs);
      return {
        // Поля перечислены, а не развёрнуты: причина отказа принадлежит
        // попытке, а не записи, и `failureReason` от прошлой неудачи не должен
        // пережить успешный исход.
        pending: {
          clientMessageId: pending.clientMessageId,
          conversationId: pending.conversationId,
          text: pending.text,
          createdAt: pending.createdAt,
          state: "retrying",
          attemptCount: pending.attemptCount + 1,
          lastAttemptAt: now,
        },
        delayMs,
      };
    }
  }
}

export interface ReplayPlan {
  /** Отправить при первой возможности. */
  readonly send: readonly PendingMessage[];
  /** Срок вышел: не отправлять, показать человеку. */
  readonly expired: readonly PendingMessage[];
  /** Ждёт человека, а не времени. */
  readonly held: readonly PendingMessage[];
}

/**
 * Что делать с тем, что лежало в хранилище, — три ведра.
 *
 * Расписание паузы жило в памяти вкладки и умерло вместе с ней, поэтому
 * восстановленная запись получает **новую** попытку: ждать паузу, отсчитанную
 * предыдущей вкладкой, нечем и незачем.
 *
 * Порядок записей сохраняется: они лежат одним хранилищем для всех бесед, и
 * восстановление, перемешавшее их, показало бы текст одной беседы в другой.
 */
export function replayPlan(records: readonly PendingMessage[], now: number): ReplayPlan {
  const send: PendingMessage[] = [];
  const expired: PendingMessage[] = [];
  const held: PendingMessage[] = [];

  for (const item of records) {
    if (item.state === "failed") {
      held.push(item);
      continue;
    }
    const restored = expire(item, now);
    if (restored.state === "failed") {
      expired.push(restored);
      continue;
    }
    send.push(restored);
  }

  return { send, expired, held };
}

/**
 * Запись в том виде, в каком она уезжает в `IndexedDB`.
 *
 * Перечень полей закрыт, и это не схема ради схемы: хранилище переживает выход
 * из системы (`clearForLogout` — единственная причина, по которой это названо
 * отдельно), и всё, что сюда попало, живёт дольше сессии. Удостоверения здесь
 * не хранятся (`ADR 0005`), а `failureReason` не уезжает, потому что причина —
 * свойство попытки, и после перезагрузки причина истечения выводится из
 * `createdAt` тем же правилом, что и до неё.
 *
 * Собирается **полями явно, без развёртывания**: развёртывание протащило бы
 * сюда любое будущее поле записи молча, и проверка состава читала бы то, что
 * утекло, а не то, что объявлено.
 */
export interface StoredPendingMessage {
  readonly clientMessageId: string;
  readonly conversationId: string;
  readonly text: string;
  readonly createdAt: number;
  /** `sending` в хранилище не попадает: запись туда кладут уже после попытки. */
  readonly state: Exclude<PendingMessage["state"], "sending">;
  readonly attemptCount: number;
  readonly lastAttemptAt: number | null;
}

export function toStored(pending: PendingMessage): StoredPendingMessage {
  return {
    clientMessageId: pending.clientMessageId,
    conversationId: pending.conversationId,
    text: pending.text,
    createdAt: pending.createdAt,
    state: pending.state === "sending" ? "retrying" : pending.state,
    attemptCount: pending.attemptCount,
    lastAttemptAt: pending.lastAttemptAt,
  };
}

/**
 * Обратно из хранилища. Причины отказа в записи нет — её и не было в перечне:
 * у восстановленной записи `failureReason` отсутствует, и это не потеря, а
 * свойство: причина истечения выводится из `createdAt` заново.
 */
export function fromStored(stored: StoredPendingMessage): PendingMessage {
  return {
    clientMessageId: stored.clientMessageId,
    conversationId: stored.conversationId,
    text: stored.text,
    createdAt: stored.createdAt,
    state: stored.state,
    attemptCount: stored.attemptCount,
    lastAttemptAt: stored.lastAttemptAt,
  };
}
