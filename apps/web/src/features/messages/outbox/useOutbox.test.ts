/**
 * `useOutbox` — жизненный цикл, а не правила: тот же контракт, что у
 * `pendingMessages.test.ts`, но здесь проверяется **проводка** — что таймер
 * действительно стреляет, что восстановление действительно шлёт попытку, что
 * `send` не позвали второй раз там, где повтора быть не должно. Ни один из
 * этих фактов не следует из чистоты `classify`/`nextAttempt` — та проверка
 * доказывает, что функция вернула правильное решение, а не что решение
 * действительно исполнено.
 *
 * Хранилище — фальшивое, `IndexedDB` в jsdom нет вовсе (см. докстринг
 * `outboxStore.ts`): реальная приёмка живучести через `IndexedDB` — предмет
 * живого прогона (перезагрузка страницы), не этого файла. Здесь проверяется
 * то, что `IndexedDB` не касается: восстановление из **любого** `OutboxStore`
 * действительно заводит попытку, а не просто заполняет список.
 *
 * Часы — фальшивые, и `waitFor` здесь намеренно не используется: он опрашивает
 * реальным `setTimeout`, который под фальшивыми часами не тикает, и тест
 * зависает, а не краснеет (тот же приём, что у `useReceipts.test.ts::flush`).
 * Вместо ожидания — `vi.advanceTimersByTimeAsync`, которая одновременно
 * продвигает часы и разбирает микрозадачи между тиками.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MessageFromJSON } from "../../../api/generated";
import type { Message } from "../../../api/generated";
import { ApiProblem, UnauthenticatedError } from "../../../api/problems";
import type { PendingMessage } from "../../../shared/lib/types";
import { useOutbox } from "./useOutbox";
import type { OutboxStore } from "./outboxStore";

function replyOf(clientMessageId: string): Message {
  return MessageFromJSON({
    message_id: "m-server",
    conversation_id: "c1",
    seq: 2,
    sender_id: "user-viewer",
    client_message_id: clientMessageId,
    type: "text",
    payload: { text: "Hello Anna" },
    created_at: "2026-09-27T14:22:31Z",
  });
}

/**
 * Хранилище в памяти — не подмена `IndexedDB`, а честная реализация того же
 * интерфейса: `put`/`list`/`remove` ведут себя так же, только без диска. Этого
 * достаточно ровно для того, что здесь проверяется, — восстановление и
 * снятие, — и недостаточно для живучести через перезагрузку, что и названо в
 * докстринге модуля.
 */
function givenMemoryStore(seed: readonly PendingMessage[] = []): OutboxStore {
  const rows = new Map(seed.map((item) => [item.clientMessageId, item]));
  return {
    put: async (pending) => {
      rows.set(pending.clientMessageId, pending);
    },
    list: async () => [...rows.values()],
    remove: async (clientMessageId) => {
      rows.delete(clientMessageId);
    },
    clearForLogout: async () => {
      rows.clear();
    },
  };
}

/** Продвигает часы и разбирает всё, что успело встать в очередь микрозадач. */
async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("повтор после сбоя сети — тот самый «после восстановления связи»", () => {
  it("первая попытка отказывает сетью, вторая — таймером — проходит сама", async () => {
    const attempts: string[] = [];
    const send = vi.fn(async (request: { clientMessageId: string }) => {
      attempts.push(request.clientMessageId);
      if (attempts.length === 1) {
        throw new TypeError("Failed to fetch");
      }
      return replyOf(request.clientMessageId);
    });
    const onSent = vi.fn();
    const store = givenMemoryStore();

    const { result } = renderHook(() => useOutbox({ store, send, onSent, now: () => 0 }));

    act(() => {
      result.current.enqueue("c1", "Hello Anna");
    });
    await flush(); // первая попытка ушла и успела отказать

    expect(send).toHaveBeenCalledTimes(1);
    expect(result.current.pending[0]?.state).toBe("retrying");

    // Ничего не происходит раньше паузы: `baseMs=1000` на первой попытке —
    // не производная величина здесь, а зафиксированный факт `RETRY_POLICY`.
    await flush(999);
    expect(send).toHaveBeenCalledTimes(1);

    await flush(1);

    // Вторая попытка — не наблюдение состояния, а вызов транспорта: таймер
    // сам, без нового нажатия, довёл дело до `send`.
    expect(send).toHaveBeenCalledTimes(2);
    expect(onSent).toHaveBeenCalledTimes(1);
    expect(onSent.mock.calls[0]?.[1].clientMessageId).toBe(attempts[0]);
  });
});

describe("окончательный отказ — сеть не виновата, повторять нечего", () => {
  it("403 останавливает очередь на первой попытке — не второй, не через час", async () => {
    const send = vi.fn(async () => {
      throw new ApiProblem({ status: 403, code: "blocked" });
    });
    const store = givenMemoryStore();

    const { result } = renderHook(() => useOutbox({ store, send, now: () => 0 }));

    act(() => {
      result.current.enqueue("c1", "Hello Anna");
    });
    await flush();

    expect(result.current.pending[0]?.state).toBe("failed");
    expect(result.current.pending[0]?.failureReason).toBe("blocked");

    // Час — заведомо больше потолка паузы (`maxMs=30s`): если бы повтор
    // случился, он случился бы здесь. Единственная попытка — не совпадение
    // окна ожидания, а то, что таймер вообще не был поставлен.
    await flush(60 * 60 * 1000);
    expect(send).toHaveBeenCalledTimes(1);
  });
});

describe("401 — очередь ждёт человека, а не время", () => {
  it("запись остаётся как есть, и час спустя send не позвали снова", async () => {
    const send = vi.fn(async () => {
      throw new UnauthenticatedError();
    });
    const store = givenMemoryStore();

    const { result } = renderHook(() => useOutbox({ store, send, now: () => 0 }));

    act(() => {
      result.current.enqueue("c1", "Hello Anna");
    });
    await flush();

    expect(send).toHaveBeenCalledTimes(1);
    // Ни `failed`, ни `retrying` дальше первой попытки — запись, которую
    // `nextAttempt` вообще не тронул (`kind: "keep"`), должна остаться такой,
    // какой видел её человек в момент нажатия «Send».
    expect(result.current.pending[0]?.attemptCount).toBe(0);

    await flush(60 * 60 * 1000);
    expect(send).toHaveBeenCalledTimes(1);
  });
});

describe("восстановление при монтировании — то, что `IndexedDB` не тронуть отсюда", () => {
  it("запись из хранилища получает свежую попытку сама, без нажатия", async () => {
    const restored: PendingMessage = {
      clientMessageId: "cm-from-disk",
      conversationId: "c1",
      text: "written before the reload",
      createdAt: 0,
      state: "retrying",
      attemptCount: 2,
      lastAttemptAt: 0,
    };
    const send = vi.fn(async (request: { clientMessageId: string }) => replyOf(request.clientMessageId));
    const onSent = vi.fn();
    const store = givenMemoryStore([restored]);

    const { result } = renderHook(() => useOutbox({ store, send, onSent, now: () => 1000 }));
    // Список приходит из хранилища асинхронно (`store.list()`), и попытка,
    // которую восстановление заводит само, — тоже: один флаш поднимает оба.
    await flush();

    expect(result.current.pending).toHaveLength(1);
    expect(result.current.pending[0]?.text).toBe("written before the reload");

    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0]?.[0]).toEqual({
      conversationId: "c1",
      clientMessageId: "cm-from-disk",
      text: "written before the reload",
    });
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it("просроченная запись из хранилища не отправляется — семь суток истекли", async () => {
    const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
    const stale: PendingMessage = {
      clientMessageId: "cm-stale",
      conversationId: "c1",
      text: "a week old",
      createdAt: 0,
      state: "retrying",
      attemptCount: 5,
      lastAttemptAt: 0,
    };
    const send = vi.fn(async (request: { clientMessageId: string }) => replyOf(request.clientMessageId));
    const store = givenMemoryStore([stale]);

    const { result } = renderHook(() =>
      useOutbox({ store, send, now: () => WEEK_MS + 1 }),
    );
    await flush();

    expect(result.current.pending).toHaveLength(1);
    expect(result.current.pending[0]?.state).toBe("failed");
    expect(result.current.pending[0]?.failureReason).toBe("expired");
    // Не вызвана вовсе — не «вызвана и отвергнута»: просроченная запись не
    // уходит на транспорт, чтобы не отправить письмо из прошлого адресату,
    // который его давно не ждёт (см. докстринг `OUTBOX_TTL_MS`).
    expect(send).not.toHaveBeenCalled();
  });

  it("нажатие Send до ответа хранилища не стирается восстановлением", async () => {
    // Гонка: `store.list()` — единственный `await` восстановления, и за это
    // время человек успевает написать и нажать Send. Мутация «восстановление
    // заменяет `pending`, а не сливает» краснит ровно здесь: свежая запись
    // исчезла бы в момент, когда пустой (на старте — ничего ещё не лежало)
    // restored-список наконец дошёл бы до `setPending`.
    const send = vi.fn(() => new Promise<Message>(() => {}));
    const store = givenMemoryStore();

    const { result } = renderHook(() => useOutbox({ store, send, now: () => 0 }));

    // До флаша: `store.list()` уже в пути, ответа ещё нет.
    act(() => {
      result.current.enqueue("c1", "typed before storage answered");
    });

    await flush();

    expect(result.current.pending).toHaveLength(1);
    expect(result.current.pending[0]?.text).toBe("typed before storage answered");
  });
});

describe("settle: снимает только подтверждённое", () => {
  it("одна из двух записей уходит, вторая остаётся нетронутой", async () => {
    const send = vi.fn(() => new Promise<Message>(() => {}));
    const store = givenMemoryStore();

    const { result } = renderHook(() => useOutbox({ store, send, now: () => 0 }));

    act(() => {
      result.current.enqueue("c1", "first");
    });
    await flush();
    act(() => {
      result.current.enqueue("c1", "second");
    });
    await flush();

    expect(result.current.pending).toHaveLength(2);
    const [first, second] = result.current.pending;

    act(() => {
      result.current.settle([first.clientMessageId]);
    });

    expect(result.current.pending).toHaveLength(1);
    expect(result.current.pending[0]?.clientMessageId).toBe(second.clientMessageId);

    // Хранилище согласно с состоянием в памяти — иначе следующее
    // восстановление вернуло бы снятую запись обратно.
    await flush();
    const stored = await store.list();
    expect(stored.map((item) => item.clientMessageId)).toEqual([second.clientMessageId]);
  });
});
