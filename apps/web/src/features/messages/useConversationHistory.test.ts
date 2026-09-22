// Детекторы обвязки ленты: два окна буферизации и отложенное намерение.
//
// Собственных решений у обвязки нет — они в `eventMerge`, `bootstrapBuffer`,
// `sync` и `history`, и там же проверяются без React. Здесь проверяется ровно
// то, чего у чистых модулей быть не может: **когда** публикация попадает в
// буфер, и что просьба догрузиться не пропадает, если её не к чему приложить.
//
// Оба окна закрыты одним и тем же буфером, и второе из них неочевидно:
// публикация с номером выше замороженной границы `T` не применяется (для
// границы она — дыра) и догрузкой не возвращается (`through_seq = T`
// ограничивает ответ). Вернуть её может только буфер. Пока буфер не покрывал
// это окно, сообщение терялось ровно в том сценарии, ради которого существует
// гейт, — и терялось молча.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiProblem } from "../../api/problems";
import type { ChatMessage } from "../../shared/lib/types";
import type { TailPage } from "./history";
import type { SyncPage, SyncPageResult } from "./sync";
import { useConversationHistory } from "./useConversationHistory";

function message(seq: number): ChatMessage {
  return {
    id: `m-${seq}`,
    seq,
    authorId: "u-1",
    kind: "text",
    text: `#${seq}`,
    timestamp: "10:00",
  };
}

function page(overrides: Partial<SyncPageResult> = {}): SyncPageResult {
  return { items: [], hasMore: false, nextAfterSeq: null, syncToSeq: null, ...overrides };
}

interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
  reject(reason: unknown): void;
}

/** Момент события задаётся вручную: гонка предъявляется, а не выигрывается. */
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function givenHistory() {
  const tail = deferred<TailPage>();
  /** Ответы догрузки раздаются по порядку — сценарий задаёт их сам. */
  const queue: Array<() => Promise<SyncPageResult>> = [];
  const syncCalls: SyncPage[] = [];
  /** Беседа, о которой спросили страницу: догрузка не должна уехать в другую. */
  const asked: string[] = [];
  const onGapDetected = vi.fn();
  const onSyncDone = vi.fn();
  const onSyncFailed = vi.fn();

  const view = renderHook(() =>
    useConversationHistory({
      conversationId: "c-1",
      loadTail: () => tail.promise,
      loadPage: (conversationId, request) => {
        asked.push(conversationId);
        syncCalls.push(request);
        // Исчерпанная очередь отдаёт «граница достигнута»: лишний запрос виден
        // в `syncCalls`, а не притворяется отказом.
        const next = queue.shift();
        return next === undefined ? Promise.resolve(page()) : next();
      },
      onGapDetected,
      onSyncDone,
      onSyncFailed,
    }),
  );

  return {
    ...view,
    tail,
    syncCalls,
    asked,
    onGapDetected,
    onSyncDone,
    onSyncFailed,
    // Ответ задаётся обещанием или изготовителем: отказ, созданный заранее,
    // успел бы пожаловаться на необработанное отклонение до того, как его
    // спросят.
    enqueue: (
      ...responses: Array<
        SyncPageResult | Promise<SyncPageResult> | (() => Promise<SyncPageResult>)
      >
    ) => {
      for (const response of responses) {
        queue.push(typeof response === "function" ? response : () => Promise.resolve(response));
      }
    },
  };
}

describe("намерение догрузиться не теряется до готовности снимка", () => {
  it("расхождение, замеченное до снимка, исполняется снимком", async () => {
    // Контрпример ревью: сокет потерян, пока `GET` хвоста ещё в пути, на
    // сервере за это время возникли 101..201, и обратно соединение приходит с
    // `wasRecovering: true, recovered: false`. Снимок ещё не применён, значит
    // молчаливый выход из догрузки — это не «нечего догружать», а потеря
    // расхождения: нового события, которое его вскрыло бы, может не быть.
    const h = givenHistory();

    await act(async () => {
      void h.result.current.startSync();
    });
    expect(h.syncCalls).toEqual([]);

    h.enqueue(page({ items: [message(101)], hasMore: false, nextAfterSeq: null }));
    await act(async () => {
      h.tail.resolve({ items: [message(100)], hasMore: true });
    });

    expect(h.syncCalls).toEqual([{ afterSeq: 100 }]);
    expect(h.result.current.appliedThroughSeq).toBe(101);
    expect(h.result.current.messages.map((it) => it.seq)).toEqual([100, 101]);
    expect(h.onSyncDone).toHaveBeenCalledTimes(1);
  });
});

describe("публикация поверх замороженной догрузки", () => {
  it("номер ровно на T+1 встаёт следом, а не теряется", async () => {
    const h = givenHistory();
    await act(async () => {
      h.tail.resolve({ items: [message(100)], hasMore: true });
    });

    // Второй ответ держим в руке: в это окно и приходит публикация.
    const second = deferred<SyncPageResult>();
    h.enqueue(
      page({ items: [message(101)], hasMore: true, nextAfterSeq: 101, syncToSeq: 102 }),
      second.promise,
    );

    await act(async () => {
      void h.result.current.startSync();
    });
    expect(h.syncCalls.at(-1)).toEqual({ afterSeq: 101, throughSeq: 102 });

    // 103 приходит, пока REST ещё дочитывает 102 — замороженная граница выше
    // его не пустит, и вернуть его догрузка не может.
    await act(async () => {
      h.result.current.acceptPublication(message(103));
    });

    await act(async () => {
      second.resolve(page({ items: [message(102)], hasMore: false, nextAfterSeq: null }));
    });

    expect(h.result.current.messages.map((it) => it.seq)).toEqual([100, 101, 102, 103]);
    expect(h.result.current.appliedThroughSeq).toBe(103);
    expect(h.asked).toEqual(["c-1", "c-1"]);
    expect(h.onSyncDone).toHaveBeenCalledTimes(1);
  });

  it("номер выше T+1 требует второго круга и не объявляет сходимость", async () => {
    // `seq = 104` при границе 102 означает, что пропущено ещё и 103. Остаться
    // в ленте без 103 — молчаливая потеря; объявить сходимость — показать
    // неполную ленту как актуальную.
    const h = givenHistory();
    await act(async () => {
      h.tail.resolve({ items: [message(100)], hasMore: true });
    });

    const second = deferred<SyncPageResult>();
    h.enqueue(
      page({ items: [message(101)], hasMore: true, nextAfterSeq: 101, syncToSeq: 102 }),
      second.promise,
      page({ items: [message(103), message(104)], hasMore: false, nextAfterSeq: null }),
    );

    await act(async () => {
      void h.result.current.startSync();
    });
    await act(async () => {
      h.result.current.acceptPublication(message(104));
    });
    await act(async () => {
      second.resolve(page({ items: [message(102)], hasMore: false, nextAfterSeq: null }));
    });

    // Второй круг идёт от новой границы, а не от исходной.
    expect(h.syncCalls).toEqual([
      { afterSeq: 100 },
      { afterSeq: 101, throughSeq: 102 },
      { afterSeq: 102 },
    ]);
    expect(h.result.current.messages.map((it) => it.seq)).toEqual([100, 101, 102, 103, 104]);
    expect(h.result.current.messages.map((it) => it.id)).toHaveLength(5);
    expect(h.result.current.appliedThroughSeq).toBe(104);
    // Сходимость объявлена один раз — после второго круга, а не после первого.
    expect(h.onSyncDone).toHaveBeenCalledTimes(1);
  });
});

describe("порядок «соединение → снимок»", () => {
  it("публикация до готовности хвоста не теряется", async () => {
    // Обратный порядок (снимок → соединение) потерял бы это сообщение
    // навсегда: у первой подписки `wasRecovering === false`, восстанавливать
    // ей нечего.
    const h = givenHistory();

    await act(async () => {
      h.result.current.acceptPublication(message(101));
    });

    await act(async () => {
      h.tail.resolve({ items: [message(100)], hasMore: true });
    });

    expect(h.result.current.messages.map((it) => it.seq)).toEqual([100, 101]);
    expect(h.result.current.appliedThroughSeq).toBe(101);
    expect(h.syncCalls).toEqual([]);
  });
});

describe("буфер снимает с себя работу, а не держит её", () => {
  it("переполнение приводит к догрузке, а не к потере", async () => {
    // Ёмкость буфера конечна, и переполнение обязано **не терять** сообщения:
    // отброшенное возвращает `after_seq`. Признак переполнения при этом не
    // липкий — иначе сходимость не наступила бы никогда.
    const h = givenHistory();
    await act(async () => {
      h.tail.resolve({ items: [message(100)], hasMore: true });
    });

    // Круг держим открытым: переполнение должно случиться **внутри** окна
    // догрузки, иначе публикации применялись бы к ленте по одной.
    const first = deferred<SyncPageResult>();
    h.enqueue(first.promise);

    await act(async () => {
      void h.result.current.startSync();
    });

    const burst = Array.from({ length: 257 }, (_, index) => message(102 + index));
    await act(async () => {
      for (const item of burst) {
        h.result.current.acceptPublication(item);
      }
    });

    // Догрузка вернёт всё, что буфер с себя снял, — от границы, а не от того,
    // что в нём уцелело.
    h.enqueue(page({ items: burst, hasMore: false, nextAfterSeq: null }));
    await act(async () => {
      first.resolve(page({ items: [message(101)], hasMore: false, nextAfterSeq: null }));
    });

    expect(h.syncCalls).toEqual([{ afterSeq: 100 }, { afterSeq: 101 }]);
    expect(h.result.current.messages.map((it) => it.seq)).toEqual(
      Array.from({ length: 259 }, (_, index) => 100 + index),
    );
    expect(new Set(h.result.current.messages.map((it) => it.id)).size).toBe(259);
    expect(h.onSyncDone).toHaveBeenCalledTimes(1);
  });
});

describe("отказ догрузки виден", () => {
  it("400 не превращается в вечное SYNCING", async () => {
    const h = givenHistory();
    await act(async () => {
      h.tail.resolve({ items: [message(100)], hasMore: true });
    });

    const failure = new ApiProblem({ status: 400, title: "Bad Request" });
    h.enqueue(() => Promise.reject(failure));

    await act(async () => {
      void h.result.current.startSync();
    });

    expect(h.onSyncFailed).toHaveBeenCalledWith(failure);
    expect(h.onSyncDone).not.toHaveBeenCalled();
  });
});
