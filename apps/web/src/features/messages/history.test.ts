// Детекторы хвоста: голова страницы, порядок в модели и пустой снимок.
//
// Форма ответа — не выдуманная: листание назад отдаёт новые первыми
// (`openapi.yaml:218-221`, `repositories/messages.py:263-275`), а надгробие
// занимает свой слот и в нумерации, и в размере страницы (`:256-259`), поэтому
// внутри страницы номера непрерывны.

import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../../shared/lib/types";
import { emptyMergeState } from "./eventMerge";
import { applyTail, TAIL_LIMIT, tailRequest } from "./history";

function message(seq: number, overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: `m-${seq}`,
    seq,
    authorId: "u-1",
    kind: "text",
    text: `#${seq}`,
    timestamp: "10:00",
    ...overrides,
  };
}

describe("голова страницы", () => {
  it("голова убывающей страницы — максимальный номер, а не последний", () => {
    // Страница `[103, 102, 101]`: последний элемент — 101, и взятая из него
    // граница увела бы и детектор пропуска, и догрузку.
    const outcome = applyTail(emptyMergeState(), { items: [message(103), message(102), message(101)], hasMore: true });

    expect(outcome.state.appliedThroughSeq).toBe(103);
  });

  it("модель получает возрастание", () => {
    const outcome = applyTail(emptyMergeState(), { items: [message(103), message(102), message(101)], hasMore: true });

    expect(outcome.state.messages.map((it) => it.seq)).toEqual([101, 102, 103]);
  });

  it("надгробие занимает свой номер, и номера остаются непрерывными", () => {
    const outcome = applyTail(emptyMergeState(), {
      items: [
        message(103),
        message(102, { deleted: true, text: "" }),
        message(101),
      ],
      hasMore: true,
    });

    expect(outcome.state.appliedThroughSeq).toBe(103);
    expect(outcome.state.messages.map((it) => it.seq)).toEqual([101, 102, 103]);
  });
});

describe("граница синхронизации из хвоста не берётся", () => {
  it("sync_to_seq листания не участвует в границе", () => {
    // У листания он `null` по контракту; даже если сервер его пришлёт, смысл
    // этого числа — не граница снимка, и подставлять его нельзя.
    const outcome = applyTail(emptyMergeState(), {
      items: [message(103), message(102)],
      hasMore: true,
      syncToSeq: 999,
    });

    expect(outcome.state.appliedThroughSeq).toBe(103);
  });
});

describe("пустой снимок и неизвестность", () => {
  it("пустая беседа сходится с границы 0", () => {
    const outcome = applyTail(emptyMergeState(), { items: [], hasMore: false });

    expect(outcome.state.appliedThroughSeq).toBe(0);
    expect(outcome.state.messages).toEqual([]);
  });

  it("до снимка граница неизвестна, и это отличимо от нуля", () => {
    expect(emptyMergeState().appliedThroughSeq).toBeNull();
  });
});

describe("запрос хвоста", () => {
  it("идёт без курсоров", () => {
    expect(tailRequest()).toEqual({ limit: TAIL_LIMIT });
  });
});
