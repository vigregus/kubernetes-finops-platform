// Отправка квитанции — четыре клетки D6, и каждая проверяется отдельно.
//
// Числа квитанции приходят из двух независимых источников: **доставлено** —
// граница слияния ленты (`appliedThroughSeq`), и от взгляда человека она не
// зависит; **прочитано** — максимум по целиком видимым строкам, и он существует
// только у видимой вкладки. Отсюда главное утверждение этих юнитов, и оно
// сформулировано точно: **в фоне уходит `delivered_seq` и не уходит `read_seq`**.
// Формулировка «в фоне отправок нет» была бы ложной — доставлено есть свойство
// устройства, а не взгляда, — и прошла бы зелёной на дефекте.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useReceipts, type UseReceiptsOptions } from "./useReceipts";

const CONVERSATION = "3f6b0d1e-0f4e-4a1f-9d2b-3ad0d1e6a111";
const DEBOUNCE = 300;

interface Receipt {
  readonly readSeq?: number;
  readonly deliveredSeq?: number;
}

interface Call {
  readonly conversationId: string;
  readonly receipt: Receipt;
}

/**
 * Вкладка ушла в фон — **браузерным событием**, а не подменой модуля: вход в
 * обвязку тот же, что в проде (`visibilitychange`), и подменять его значило бы
 * проверять не тот код, который исполняется.
 *
 * Свойство определяется собственным, потому что в `Document.prototype` оно
 * живёт геттером: присваивание молча ничего не сделает, и тест был бы зелёным
 * на неработающем правиле.
 */
function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

function givenReceipts(
  initial: { delivered: number | null; visible: number },
  options: Partial<UseReceiptsOptions> = {},
) {
  const calls: Call[] = [];

  const view = renderHook(
    ({ delivered, visible }: { delivered: number | null; visible: number }) =>
      useReceipts({
        conversationId: CONVERSATION,
        deliveredThroughSeq: delivered,
        visibleThroughSeq: visible,
        send: async (conversationId, receipt) => {
          calls.push({ conversationId, receipt });
        },
        debounceMs: DEBOUNCE,
        ...options,
      }),
    { initialProps: initial },
  );

  /** Дребезг истёк — как по часам, а не по реальному ожиданию. */
  async function flush() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEBOUNCE + 1);
    });
  }

  return { ...view, calls, flush };
}

beforeEach(() => {
  vi.useFakeTimers();
  setVisibility("visible");
});

afterEach(() => {
  vi.useRealTimers();
  // Возврат именно в исходное: `visibilityState` живёт в прототипе документа, и
  // оставленное собственное свойство изменило бы окружение следующим файлам.
  delete (document as { visibilityState?: unknown }).visibilityState;
});

describe("что уходит в запрос квитанции", () => {
  it("применённое сообщение при невидимой строке: уходит только доставка", async () => {
    const { calls, flush, result } = givenReceipts({ delivered: 7, visible: 0 });

    await flush();

    expect(calls).toStrictEqual([
      { conversationId: CONVERSATION, receipt: { deliveredSeq: 7 } },
    ]);
    // Возвращается **отправленное**, и `read_seq` в нём нет вовсе: ноль здесь
    // значил бы «прочитано ни до чего», то есть утверждение, которого вкладка
    // не делала. Запрос разрешает одно поле из двух (`anyOf`), и этой же
    // формой пользуется разметка: `data-my-read-seq` не появляется, пока
    // прочтение не отправлено.
    expect(result.current).toStrictEqual({ deliveredSeq: 7 });
  });

  it("целиком видимая строка двигает и прочтение", async () => {
    const { calls, flush, result } = givenReceipts({ delivered: 7, visible: 5 });

    await flush();

    expect(calls).toStrictEqual([
      { conversationId: CONVERSATION, receipt: { readSeq: 5, deliveredSeq: 7 } },
    ]);
    expect(result.current).toStrictEqual({ readSeq: 5, deliveredSeq: 7 });
  });

  it("то же число второй раз не уходит", async () => {
    const { calls, flush, rerender } = givenReceipts({ delivered: 7, visible: 5 });
    await flush();

    rerender({ delivered: 7, visible: 5 });
    await flush();

    expect(calls).toHaveLength(1);
  });

  it("отставшее число не уходит — ни полем, ни запросом", async () => {
    // Граница может уменьшиться: сверка заменяет список ответом REST, а
    // вкладка, вернувшаяся из сна, видит меньше видимого, чем видела до него.
    // Отправка назад откатила бы отметку собеседника (D6б).
    const { calls, flush, rerender, result } = givenReceipts({ delivered: 9, visible: 7 });
    await flush();

    rerender({ delivered: 9, visible: 3 });
    await flush();

    expect(calls).toHaveLength(1);
    expect(result.current).toStrictEqual({ readSeq: 7, deliveredSeq: 9 });
  });

  it("пустая страница не шлёт ничего: границы ещё нет", async () => {
    // `null` — «снимка не было», и это не ноль: отправить `{delivered_seq: 0}`
    // значило бы объявить доставку, о которой сервер ещё не отвечал.
    const { calls, flush, result } = givenReceipts({ delivered: null, visible: 0 });

    await flush();

    expect(calls).toHaveLength(0);
    expect(result.current).toBeNull();
  });
});

describe("вкладка в фоне: доставка идёт, прочтение — нет", () => {
  it("в фоне уходит delivered_seq, а read_seq — нет", async () => {
    const { calls, flush, rerender } = givenReceipts({ delivered: 5, visible: 5 });
    await flush();

    setVisibility("hidden");
    // Сообщение применено и в фоне: публикация доходит до свёрнутой вкладки и
    // применяется — таймеры тормозятся, но JS исполняется. Доставлено есть
    // свойство **устройства**, а не взгляда, и запрет «в фоне не отправляем
    // ничего» отнял бы у RCP-001 половину смысла.
    rerender({ delivered: 9, visible: 9 });
    await flush();

    expect(calls).toHaveLength(2);
    expect(calls[1]!.receipt).toStrictEqual({ deliveredSeq: 9 });
  });

  it("возврат вкладки отправляет свёрнутое прочтение, и назад ничего не идёт", async () => {
    const { calls, flush, rerender } = givenReceipts({ delivered: 5, visible: 5 });
    await flush();

    setVisibility("hidden");
    rerender({ delivered: 9, visible: 9 });
    await flush();

    setVisibility("visible");
    await flush();

    expect(calls).toHaveLength(3);
    // Свёрнутое — то есть максимум за время отсутствия, а не первое число после
    // возврата: иначе прочитанным осталось бы всё, что человек не видел.
    expect(calls[2]!.receipt).toStrictEqual({ readSeq: 9 });
    // И ни одно из двух чисел не пошло назад: последовательность доставки
    // строго возрастает, прочтение — тоже.
    expect(calls.map((call) => call.receipt.deliveredSeq ?? 0)).toEqual([5, 9, 0]);
    expect(calls.map((call) => call.receipt.readSeq ?? 0)).toEqual([5, 0, 9]);
  });

  it("наблюдатель, молчавший в фоне, довозит прочтение на возврате", async () => {
    // Второй законный ход событий: браузер откладывает уведомления наблюдателя
    // до возврата вкладки. Вкладка при этом уже видима, и число приходит после
    // события `visibilitychange` — правило то же: пока в фоне, `read_seq` не
    // уходит; как только вкладка видима — уходит.
    const { calls, flush, rerender } = givenReceipts({ delivered: 5, visible: 0 });
    await flush();
    expect(calls).toHaveLength(1);

    setVisibility("hidden");
    rerender({ delivered: 9, visible: 0 });
    await flush();

    setVisibility("visible");
    rerender({ delivered: 9, visible: 9 });
    await flush();

    expect(calls.map((call) => call.receipt)).toStrictEqual([
      { deliveredSeq: 5 },
      { deliveredSeq: 9 },
      { readSeq: 9 },
    ]);
  });
});

describe("отказ отправки", () => {
  it("отказ не теряет число: следующий повод шлёт максимум", async () => {
    // Транспорт без гарантий: запрос может не дойти. Число при этом не теряется
    // не потому, что мы его запомнили в очереди, — очередь здесь не нужна, — а
    // потому что отправляемое число есть **максимум** положения вкладки, и
    // следующий повод несёт его же, а не только своё приращение.
    const send = vi
      .fn<(conversationId: string, receipt: Receipt) => Promise<unknown>>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(undefined);

    const { flush, rerender, result } = givenReceipts(
      { delivered: 7, visible: 0 },
      { send },
    );
    await flush();

    // Отказ не записался в отправленное — и разметка этого не утверждает.
    expect(result.current).toBeNull();

    rerender({ delivered: 10, visible: 0 });
    await flush();

    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[1]![1]).toStrictEqual({ deliveredSeq: 10 });
    expect(result.current).toStrictEqual({ deliveredSeq: 10 });
  });
});
