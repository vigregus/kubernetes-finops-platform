// Детекторы слияния: пропуск в нумерации, дубль на стыке двух путей и
// единственная точка порядка.
//
// Живой дыры в `seq` на стенде не бывает: у канала один издатель с монотонным
// `conversation_seq`, а восстановление проигрывает историю. Поэтому `MSG-005`
// доказывается **здесь** — на входе детектора, — а браузерный сценарий
// доказывает общий путь починки. Разделение названо в отчёте гейта.

import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../../shared/lib/types";
import {
  applyBuffered,
  applyMessage,
  applyPage,
  applySnapshot,
  emptyMergeState,
  type MergeState,
} from "./eventMerge";

let counter = 0;

function message(seq: number, id = `m-${++counter}`): ChatMessage {
  return { id, seq, authorId: "u-1", kind: "text", text: `#${seq}`, timestamp: "10:00" };
}

/** Снимок применён: граница известна, лента — то, что в ней лежит. */
function givenState(appliedThroughSeq: number, messages: ChatMessage[] = []): MergeState {
  return { messages, appliedThroughSeq };
}

function seqs(state: MergeState): number[] {
  return state.messages.map((it) => it.seq);
}

describe("пропуск в нумерации", () => {
  it("событие выше границы на единицу не применяется", () => {
    const state = givenState(100, [message(100)]);

    const outcome = applyMessage(state, message(102));

    expect(outcome.kind).toBe("gap");
    expect(seqs(outcome.state)).toEqual([100]);
  });

  it("пропуск не двигает применённую границу", () => {
    // Граница растёт только на применённом сообщении. Сдвинь её вперёд — и
    // догрузка запросит `after_seq` за дырой, то есть пропущенное не вернётся
    // уже никогда.
    const state = givenState(100, [message(100)]);

    const outcome = applyMessage(state, message(102));

    expect(outcome.state.appliedThroughSeq).toBe(100);
  });

  it("следующий по порядку номер применяется и двигает границу", () => {
    const state = givenState(100, [message(100)]);

    const outcome = applyMessage(state, message(101));

    expect(outcome.kind).toBe("applied");
    expect(outcome.state.appliedThroughSeq).toBe(101);
    expect(seqs(outcome.state)).toEqual([100, 101]);
  });

  it("догрузка, начинающаяся выше границы, — та же дыра", () => {
    const state = givenState(100, [message(100)]);

    const outcome = applyPage(state, [message(103), message(104)]);

    expect(outcome.kind).toBe("gap");
    expect(outcome.state.appliedThroughSeq).toBe(100);
  });

  it("проигрывание буфера останавливается на первой дыре", () => {
    const state = givenState(100, [message(100)]);

    const outcome = applyBuffered(state, [message(103), message(101)]);

    expect(outcome.kind).toBe("gap");
    expect(seqs(outcome.state)).toEqual([100, 101]);
    expect(outcome.state.appliedThroughSeq).toBe(101);
  });
});

describe("ровно один раз", () => {
  it("дубль по message_id не удваивает ленту", () => {
    const state = givenState(100, [message(100)]);
    const again = message(101);

    const first = applyMessage(state, again);
    const second = applyMessage(first.state, again);

    expect(second.kind).toBe("duplicate");
    expect(seqs(second.state)).toEqual([100, 101]);
  });

  it("номер не выше применённой границы не применяется повторно", () => {
    // Так выглядит сообщение, уже пришедшее снимком и повторённое каналом.
    const state = givenState(100, [message(100)]);

    const outcome = applyMessage(state, message(100));

    expect(outcome.kind).toBe("already-applied");
    expect(seqs(outcome.state)).toEqual([100]);
  });

  it("догрузка применяет только то, чего в ленте нет", () => {
    const state = givenState(101, [message(101)]);

    const outcome = applyPage(state, [message(102), message(103)]);

    expect(outcome.kind).toBe("applied");
    expect(seqs(outcome.state)).toEqual([101, 102, 103]);
    expect(outcome.state.appliedThroughSeq).toBe(103);
  });
});

describe("единственная точка порядка", () => {
  it("убывающий хвост приходит в модель по возрастанию", () => {
    // `openapi.yaml:218-221`: листание назад отдаёт новые первыми. Голова —
    // `max(seq)`, а не последний элемент массива.
    const outcome = applySnapshot(emptyMergeState(), [message(103), message(102), message(101)]);

    expect(seqs(outcome.state)).toEqual([101, 102, 103]);
    expect(outcome.state.appliedThroughSeq).toBe(103);
  });

  it("публикация и догрузка не расходятcя по порядку", () => {
    const snapshot = applySnapshot(emptyMergeState(), [message(102), message(101)]);
    const published = applyMessage(snapshot.state, message(103));

    const outcome = applyPage(published.state, [message(104)]);

    expect(seqs(outcome.state)).toEqual([101, 102, 103, 104]);
  });
});

describe("граница: неизвестна, ноль и число", () => {
  it("до снимка граница неизвестна, и применять нечего", () => {
    const outcome = applyMessage(emptyMergeState(), message(1));

    expect(outcome.kind).toBe("before-snapshot");
    expect(outcome.state.appliedThroughSeq).toBeNull();
    expect(outcome.state.messages).toEqual([]);
  });

  it("пустой снимок даёт границу 0, и первое сообщение применяется", () => {
    // Не «null»: пустая страница при непустом лимите означает ровно одно —
    // сообщений нет, и следующий законный номер единица. Иначе первое
    // сообщение читалось бы пропуском `1 > null + 1`, и пустая беседа не
    // сошлась бы никогда.
    const snapshot = applySnapshot(emptyMergeState(), []);

    expect(snapshot.state.appliedThroughSeq).toBe(0);

    const outcome = applyMessage(snapshot.state, message(1));

    expect(outcome.kind).toBe("applied");
    expect(outcome.state.appliedThroughSeq).toBe(1);
  });

  it("пустой снимок поверх непустой ленты не обнуляет границу", () => {
    // Пустая страница догрузки — не «беседа пуста»: границу она не сдвигает.
    const state = givenState(101, [message(101)]);

    const outcome = applyPage(state, []);

    expect(outcome.kind).toBe("applied");
    expect(outcome.state.appliedThroughSeq).toBe(101);
  });
});
