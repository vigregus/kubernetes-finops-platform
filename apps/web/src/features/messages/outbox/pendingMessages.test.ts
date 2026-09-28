// Очередь офлайна — четыре чистые правила и одна закрытая схема записи.
//
// Проверяются здесь именно правила, а не хранилище: `IndexedDB` в `jsdom` нет
// вовсе, и проверка, притворяющаяся проверкой хранилища, доказывала бы работу
// подставного объекта. Что хранится — предмет `toStored` (чистая функция), чем
// хранится — предмет живой приёмки.
//
// Классификация отказа — не украшение: без неё повтор идёт и на `403`, то есть
// очередь бесконечно повторяет отказ, на который сервер уже ответил «нет».

import { describe, expect, it } from "vitest";

import { ApiProblem, ServiceUnavailableError, SessionExpiredError } from "../../../api/problems";
import type { PendingMessage } from "../../../shared/lib/types";
import {
  OUTBOX_TTL_MS,
  RETRY_POLICY,
  classify,
  expire,
  fromStored,
  isExpired,
  nextAttempt,
  replayPlan,
  toStored,
} from "./pendingMessages";

/** Запись очереди в том виде, в каком её кладёт отправка. */
function record(overrides: Partial<PendingMessage> = {}): PendingMessage {
  return {
    clientMessageId: "8a1f0c2e-6f4b-4a1e-9c2d-0f7b5a3e1d40",
    conversationId: "b0c9e5a2-3d18-4f77-8a5e-2c1d9f0b6e33",
    text: "привет",
    createdAt: 1_700_000_000_000,
    state: "sending",
    attemptCount: 0,
    lastAttemptAt: null,
    ...overrides,
  };
}

describe("classify: отказ решает судьбу записи", () => {
  it("403 — окончательный: повторять отказ бессмысленно", () => {
    // Заблокирован, адрес не подтверждён — сервер сказал «нет» и на повторе
    // скажет то же. Отнесённый к повторяемым, этот отказ крутился бы в очереди
    // вечно, потому что условия, при которых он пройдёт, не наступят.
    expect(classify(new ApiProblem({ status: 403, code: "user_blocked" }))).toEqual({
      kind: "final",
      reason: "user_blocked",
    });
  });

  it("404, 400 и 422 — тоже окончательные, каждый со своей причиной", () => {
    expect(classify(new ApiProblem({ status: 404, code: "resource_not_found" }))).toEqual({
      kind: "final",
      reason: "resource_not_found",
    });
    expect(classify(new ApiProblem({ status: 400, code: "invalid_request" }))).toEqual({
      kind: "final",
      reason: "invalid_request",
    });
    expect(classify(new ApiProblem({ status: 422, code: "unprocessable" }))).toEqual({
      kind: "final",
      reason: "unprocessable",
    });
  });

  it("отказ без code не остаётся без причины: подставляется замена", () => {
    // `code` схема не требует, и «Not sent» без причины человеку ничего не
    // объясняет. Замена называется замена, а не выдаётся за слово сервера.
    expect(classify(new ApiProblem({ status: 403 }))).toEqual({ kind: "final", reason: "rejected" });
  });

  it("429 — повторяемый, и пауза берётся у сервера, а не выдумывается", () => {
    // Без `Retry-After` клиент повторяет вслепую и упирается снова — довод
    // дословно серверный (`api/main.py`: «Без Retry-After клиент повторяет
    // вслепую»).
    expect(classify(new ApiProblem({ status: 429, retryAfterSeconds: 45 }))).toEqual({
      kind: "retry",
      retryAfterMs: 45_000,
    });
  });

  it("429 без заголовка — повторяемый, но пауза назначается своя", () => {
    // Заголовок в контракте есть, а обязательным не объявлен: `Retry-After`
    // отсутствующий — не «ждать ноль», а «сервер не сказал».
    expect(classify(new ApiProblem({ status: 429 }))).toEqual({ kind: "retry", retryAfterMs: null });
  });

  it("500 и 503 — повторяемые: сервер сломался, а не отказал", () => {
    expect(classify(new ApiProblem({ status: 500 }))).toEqual({ kind: "retry", retryAfterMs: null });
    // `503` до `ApiProblem` не доходит: `client.check` разбирает его в
    // `ServiceUnavailableError` раньше. Проверяются оба входа — иначе ветка
    // «5xx повторяем» держалась бы на одном из двух.
    expect(classify(new ServiceUnavailableError("Сервис недоступен"))).toEqual({
      kind: "retry",
      retryAfterMs: null,
    });
  });

  it("сеть не ответила вовсе — повторяемый", () => {
    // `client.send` превращает сетевой отказ в `ServiceUnavailableError`
    // («Response не приходит вовсе»). Это самый частый случай офлайна.
    expect(classify(new ServiceUnavailableError("Сервис недоступен"))).toEqual({
      kind: "retry",
      retryAfterMs: null,
    });
  });

  it("401 — ни повтор, ни отказ: очередь сохраняется", () => {
    // Стирание очереди на `401` означало бы, что написанное пропало на
    // перелогине: человек ушёл на вход и вернулся, а текста уже нет. Очередь —
    // не мусор и не отправленное, и третий класс заведён ровно за этим.
    expect(classify(new SessionExpiredError())).toEqual({ kind: "keep" });
    // Второй вход того же класса: `401` до `ready` и `401` после — разные
    // состояния интерфейса, но одна судьба записи.
    expect(classify(new ApiProblem({ status: 401 }))).toEqual({ kind: "keep" });
  });

  it("неожиданный отказ — повторяемый: выбросить запись можно только по слову сервера", () => {
    // Запись — единственная копия того, что человек написал. Отбросить её
    // даёт право определённый ответ сервера («это не пройдёт»), а не сбой в
    // нашем коде: цена ошибки в эту сторону — запись, повисев до срока, станет
    // `failed`, и это видно; цена в обратную — потерянный текст.
    expect(classify(new TypeError("boom"))).toEqual({ kind: "retry", retryAfterMs: null });
  });
});

describe("expire: срок — это условие, а не таймер", () => {
  it("свежая запись не меняется — и остаётся тем же объектом", () => {
    // Тождество, а не равенство: правило, пересобирающее запись на каждом
    // чтении, стирало бы поле, которого оно не знает.
    const fresh = record();
    expect(expire(fresh, fresh.createdAt + OUTBOX_TTL_MS - 1)).toBe(fresh);
    expect(isExpired(fresh, fresh.createdAt + OUTBOX_TTL_MS - 1)).toBe(false);
  });

  it("граница срока: последняя миллисекунда жива, следующая — нет", () => {
    const fresh = record();
    expect(isExpired(fresh, fresh.createdAt + OUTBOX_TTL_MS)).toBe(true);
  });

  it("просроченная становится failed с причиной expired, текст и адрес остаются", () => {
    // Человек об этом узнаёт надписью, а не тишиной: «Not sent — message
    // expired». Отправки по истечении срока не бывает — семидневная пауза не
    // повод отправить, а повод сказать.
    const expired = expire(record(), record().createdAt + OUTBOX_TTL_MS);
    expect(expired.state).toBe("failed");
    expect(expired.failureReason).toBe("expired");
    expect(expired.clientMessageId).toBe(record().clientMessageId);
    expect(expired.text).toBe("привет");
  });

  it("окончательно отвергнутая не переписывается сроком", () => {
    // Причина у неё уже есть, и подменять её на «expired» значило бы объявить
    // истёкшим то, что сервер отверг по существу.
    const rejected = record({ state: "failed", failureReason: "user_blocked" });
    expect(expire(rejected, rejected.createdAt + OUTBOX_TTL_MS * 2)).toBe(rejected);
  });
});

describe("nextAttempt: срок и пауза следующей попытки", () => {
  it("повтор берёт идентификатор из записи, а не чеканит новый", () => {
    // Единственное, что отличает идемпотентный повтор от повторной отправки:
    // с новым `client_message_id` сервер заведёт **второе** сообщение, и в
    // ленте окажутся обе копии.
    const first = record({ state: "retrying", attemptCount: 1, lastAttemptAt: 1 });
    const again = nextAttempt(first, { kind: "retry", retryAfterMs: null }, 2);
    expect(again.pending.clientMessageId).toBe(first.clientMessageId);
    expect(again.pending.attemptCount).toBe(2);
    expect(again.pending.lastAttemptAt).toBe(2);
    expect(again.pending.state).toBe("retrying");
  });

  it("пауза растёт с числом попыток и упирается в потолок", () => {
    const delay = (attemptCount: number) =>
      nextAttempt(
        record({ state: "retrying", attemptCount }),
        { kind: "retry", retryAfterMs: null },
        0,
      ).delayMs;
    expect(delay(0)).toBe(RETRY_POLICY.baseMs);
    expect(delay(1)).toBe(RETRY_POLICY.baseMs * 2);
    expect(delay(20)).toBe(RETRY_POLICY.maxMs);
  });

  it("пауза сервера сильнее своей, но своя пауза ею не сокращается", () => {
    const withServer = (retryAfterMs: number) =>
      nextAttempt(record({ state: "retrying" }), { kind: "retry", retryAfterMs }, 0).delayMs;
    expect(withServer(45_000)).toBe(45_000);
    expect(withServer(1)).toBe(RETRY_POLICY.baseMs);
  });

  it("окончательный отказ: причины попытки нет, и повтора не будет", () => {
    const rejected = nextAttempt(
      record({ state: "retrying", attemptCount: 3 }),
      { kind: "final", reason: "user_blocked" },
      10,
    );
    expect(rejected.pending.state).toBe("failed");
    expect(rejected.pending.failureReason).toBe("user_blocked");
    expect(rejected.delayMs).toBeNull();
  });

  it("401 не трогает запись вовсе: ни счётчика, ни состояния, ни паузы", () => {
    // Попытка была, но исхода у неё нет: очередь ждёт человека, а не времени.
    // Счётчик здесь двигался бы вместе с паузой, и очередь начала бы считать
    // попытки, которых она не делала.
    const held = record({ state: "retrying", attemptCount: 2, lastAttemptAt: 5 });
    const outcome = nextAttempt(held, { kind: "keep" }, 99);
    expect(outcome.pending).toBe(held);
    expect(outcome.delayMs).toBeNull();
  });
});

describe("replayPlan: восстановление после перезагрузки", () => {
  it("просроченная не уходит в отправку", () => {
    // Соблазн восстановить всё, что лежит, велик — и он же отправлял бы
    // сообщение недельной давности, которого человек давно не ждёт.
    const now = 1_700_000_000_000 + OUTBOX_TTL_MS;
    const plan = replayPlan(
      [record({ state: "retrying", createdAt: 1_700_000_000_000 })],
      now,
    );
    expect(plan.send).toEqual([]);
    expect(plan.expired).toHaveLength(1);
    expect(plan.expired[0]?.failureReason).toBe("expired");
    expect(plan.held).toEqual([]);
  });

  it("непросроченная уходит в отправку с тем же идентификатором", () => {
    const now = 1_700_000_000_000 + OUTBOX_TTL_MS - 1;
    const plan = replayPlan([record({ state: "retrying", attemptCount: 3 })], now);
    expect(plan.send.map((item) => item.clientMessageId)).toEqual([record().clientMessageId]);
    expect(plan.send[0]?.attemptCount).toBe(3);
    expect(plan.expired).toEqual([]);
  });

  it("окончательно отвергнутая не отправляется и сроком не переписывается", () => {
    const now = 1_700_000_000_000 + OUTBOX_TTL_MS * 3;
    const rejected = record({ state: "failed", failureReason: "user_blocked" });
    const plan = replayPlan([rejected], now);
    expect(plan.send).toEqual([]);
    expect(plan.expired).toEqual([]);
    expect(plan.held).toEqual([rejected]);
  });

  it("порядок записей сохраняется, и беседа не путается", () => {
    // Записи разных бесед лежат в одном хранилище, и восстановление,
    // перемешавшее их, показало бы текст одной беседы в другой.
    const now = 1_700_000_000_000;
    const first = record({ clientMessageId: "a", conversationId: "c1", state: "retrying" });
    const second = record({ clientMessageId: "b", conversationId: "c2", state: "retrying" });
    const plan = replayPlan([first, second], now);
    expect(plan.send.map((item) => item.clientMessageId)).toEqual(["a", "b"]);
    expect(plan.send.map((item) => item.conversationId)).toEqual(["c1", "c2"]);
  });
});

describe("toStored: перечень полей закрыт", () => {
  // Список выписан здесь **дословно**, а не взят из модуля: проверка,
  // сравнивающая перечень с самим собой, проходит и когда в хранилище
  // положили поле, а в перечень его дописали.
  const CLOSED_FIELDS = [
    "attemptCount",
    "clientMessageId",
    "conversationId",
    "createdAt",
    "lastAttemptAt",
    "state",
    "text",
  ];

  it("в хранилище уезжают ровно эти ключи и ни одного сверх", () => {
    // Проверка читает **ключи**, а не значения: удостоверение в записи
    // выглядит как обычное поле с обычной строкой, и по значениям его не
    // отличить от `text`.
    expect(Object.keys(toStored(record({ state: "retrying" }))).sort()).toEqual(CLOSED_FIELDS);
  });

  it("удостоверения в записи нет — ни токена, ни cookie", () => {
    // `ADR 0005` запрещает удостоверения в `localStorage`; у очереди причина
    // та же, и она не про место, а про суть: запись переживает выход из
    // системы, а удостоверение — нет.
    const stored = toStored(record({ state: "retrying" }));
    expect(JSON.stringify(stored)).not.toMatch(/token|refresh|credential|cookie|authorization/i);
  });

  it("причина отказа в хранилище не уезжает: она принадлежит попытке", () => {
    // Перечень закрыт семью полями, и `failureReason` в нём нет. После
    // перезагрузки запись показывается без причины — «Not sent», а причина
    // истечения выводится из `createdAt` тем же правилом, что и до неё.
    const stored = toStored(record({ state: "failed", failureReason: "user_blocked" }));
    expect("failureReason" in stored).toBe(false);
    expect(Object.keys(stored).sort()).toEqual(CLOSED_FIELDS);
  });

  it("круг через хранилище сохраняет всё, что в перечне, и ничего не выдумывает", () => {
    // Обратный ход нужен затем, чтобы поле, забытое в `fromStored`, не
    // оказалось стёртым: запись, у которой пропало `attemptCount`, начинает
    // считать попытки заново и повторяет чаще, чем собиралась.
    const original = record({ state: "retrying", attemptCount: 4, lastAttemptAt: 17 });
    expect(fromStored(toStored(original))).toEqual(original);
  });

  it("восстановленная запись причины не несёт — и это свойство, а не потеря", () => {
    const stored = toStored(record({ state: "failed", failureReason: "user_blocked" }));
    expect("failureReason" in fromStored(stored)).toBe(false);
  });
});
