// Детекторы хвоста: голова страницы, порядок в модели и пустой снимок.
//
// Форма ответа — не выдуманная: листание назад отдаёт новые первыми
// (`openapi.yaml:218-221`, `repositories/messages.py:263-275`), а надгробие
// занимает свой слот и в нумерации, и в размере страницы (`:256-259`), поэтому
// внутри страницы номера непрерывны.

import { describe, expect, it } from "vitest";

import type { ListMessages200Response, Message as MessageDto } from "../../api/generated";
import type { ChatMessage } from "../../shared/lib/types";
import { emptyMergeState } from "./eventMerge";
import { applyTail, TAIL_LIMIT, tailPageOf, tailRequest } from "./history";

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

const NOW = new Date("2026-09-20T18:00:00Z");
const EN = { locale: "en-US", timeZone: "UTC" } as const;

function dto(seq: number): MessageDto {
  return {
    messageId: `m-${seq}`,
    conversationId: "c-1",
    seq,
    senderId: "u-1",
    type: "text",
    payload: { text: `#${seq}` },
    createdAt: "2026-09-20T14:22:31Z",
  };
}

function response(overrides: Partial<ListMessages200Response> = {}): ListMessages200Response {
  // `syncToSeq` в ответе обязателен (`number | null`) — у листания назад он
  // `null`, и это значение по умолчанию для листающего запроса.
  return { items: [], hasMore: false, syncToSeq: null, ...overrides };
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

describe("форма ответа на входе", () => {
  it("страница собирается из ответа: записи переводятся, форма остаётся", () => {
    // Форма ответа читается один раз и здесь: сколько элементов, есть ли
    // продолжение, какая граница. Собранная на месте вызова, она собиралась бы
    // дважды — здесь и в продолжении догрузки.
    const page = tailPageOf(
      response({
        items: [dto(103), dto(102), dto(101)],
        hasMore: true,
        nextBeforeSeq: 101,
        syncToSeq: null,
      }),
      "u-viewer",
      NOW,
      EN,
    );

    expect(page.items.map((it) => it.id)).toEqual(["m-103", "m-102", "m-101"]);
    expect(page.hasMore).toBe(true);
    expect(page.nextBeforeSeq).toBe(101);
    expect(page.syncToSeq).toBeNull();
  });

  it("настоящий ответ проходит до головы и порядка целиком", () => {
    // Сквозная проверка формы: ответ сервера входит как есть, а на выходе —
    // граница 103 и лента в возрастании. Именно здесь сходятся обе ловушки
    // контракта — убывание листания и голова как `max(seq)`.
    const page = tailPageOf(
      response({ items: [dto(103), dto(102), dto(101)], hasMore: true, syncToSeq: null }),
      "u-viewer",
      NOW,
      EN,
    );
    const outcome = applyTail(emptyMergeState(), page);

    expect(outcome.state.appliedThroughSeq).toBe(103);
    expect(outcome.state.messages.map((it) => it.seq)).toEqual([101, 102, 103]);
    // И время — display-строка, а не ISO: адаптер вызывается на каждой записи.
    expect(outcome.state.messages[0].timestamp).toBe("14:22");
  });

  it("пустой ответ — успешный снимок пустой беседы, а не отсутствие истории", () => {
    const outcome = applyTail(emptyMergeState(), tailPageOf(response({ items: [] }), "u-viewer", NOW, EN));

    expect(outcome.state.appliedThroughSeq).toBe(0);
    expect(outcome.state.messages).toEqual([]);
  });
});
