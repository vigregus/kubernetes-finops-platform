// Детекторы хвоста: голова страницы, порядок в модели и пустой снимок.
//
// Форма ответа — не выдуманная: листание назад отдаёт новые первыми
// (`openapi.yaml:218-221`, `repositories/messages.py:263-275`), а надгробие
// занимает свой слот и в нумерации, и в размере страницы (`:256-259`), поэтому
// внутри страницы номера непрерывны.

import { describe, expect, it } from "vitest";

import type {
  ListMessages200Response,
  ListMessagesRequest,
  Message as MessageDto,
} from "../../api/generated";
import type { ChatMessage } from "../../shared/lib/types";
import { emptyMergeState } from "./eventMerge";
import type { HistoryApi } from "./history";
import { applyTail, createHistorySource, TAIL_LIMIT, tailPageOf, tailRequest } from "./history";

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

describe("загрузчики истории поверх клиента", () => {
  // Красный здесь приходит от двух классов дефекта, и оба не выдуманы:
  // «параметр не доехал» — ровно тот класс, которым был блокер ревизии 3.2
  // (`conversationId` терялся по дороге в URL), и «граница не заморожена» —
  // единственный способ, которым догрузка уезжает за голову.

  function givenApi(
    answer: ListMessages200Response = response({
      items: [dto(103), dto(102), dto(101)],
      hasMore: true,
      syncToSeq: null,
    }),
  ) {
    const calls: ListMessagesRequest[] = [];
    const api: HistoryApi = {
      listMessages: (request) => {
        calls.push(request);

        return Promise.resolve(answer);
      },
    };

    return { api, calls };
  }

  function givenSource(answer?: ListMessages200Response) {
    const { api, calls } = givenApi(answer);

    return {
      calls,
      source: createHistorySource({
        api,
        currentUserId: "u-viewer",
        now: () => NOW,
        timestampOptions: EN,
      }),
    };
  }

  it("хвост спрашивается у той беседы, которую открыли", async () => {
    // `toStrictEqual`, а не `toEqual`: последний пропускает ключи со значением
    // `undefined`, и запрос, собранный с лишним курсором, остался бы зелёным.
    // У листания курсоров нет вовсе — только лимит.
    const { source, calls } = givenSource();

    const page = await source.loadTail("c-1");

    expect(calls).toStrictEqual([{ conversationId: "c-1", limit: TAIL_LIMIT }]);
    // Записи проходят **как пришли** — в порядке ответа, то есть по убыванию.
    // Порядок наводится ровно в одном месте, `eventMerge` (B22); сортировка
    // здесь завела бы второе место для того же самого, и однажды они
    // разошлись бы молча — на том сообщении, ради которого и затевался гейт.
    expect(page.items.map((it) => it.id)).toEqual(["m-103", "m-102", "m-101"]);
  });

  it("догрузка несёт замороженную границу и переносит её в ответ", async () => {
    // Граница переносится **как есть**: потеряй её загрузчик — `advance`
    // оставит курсор незамороженным, и следующий запрос пересчитает границу
    // заново, то есть догрузка поедет за голову.
    const { source, calls } = givenSource(
      response({ items: [dto(103)], hasMore: false, syncToSeq: 102, nextAfterSeq: null }),
    );

    const result = await source.loadPage("c-1", { afterSeq: 100, throughSeq: 102 });

    expect(calls).toStrictEqual([
      { conversationId: "c-1", afterSeq: 100, throughSeq: 102, limit: TAIL_LIMIT },
    ]);
    expect(result.syncToSeq).toBe(102);
    expect(result.hasMore).toBe(false);
    expect(result.nextAfterSeq).toBeNull();
  });

  it("первый запрос догрузки не несёт числа на месте границы", async () => {
    // Требование сформулировано по проводу, а не по форме объекта: клиент
    // отправляет `through_seq` только при `!= null`, поэтому «ключ со
    // значением `undefined`» и «ключа нет» для сервера — одно и то же, и
    // различать их в тесте значило бы требовать от кода того, чего контракт
    // не требует. Требует он другого: **числа** на месте границы быть не
    // должно — `through_seq=0` при `after_seq=100` это `400`
    // (`openapi.yaml:608-613`), то есть смерть догрузки на первом же запросе.
    const { source, calls } = givenSource();

    await source.loadPage("c-1", { afterSeq: 100 });

    expect(calls[0].conversationId).toBe("c-1");
    expect(calls[0].afterSeq).toBe(100);
    expect(calls[0].throughSeq).toBeUndefined();
    expect(calls[0].limit).toBe(TAIL_LIMIT);
  });
});
