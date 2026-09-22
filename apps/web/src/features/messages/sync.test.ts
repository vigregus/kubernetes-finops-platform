// Детекторы протокола догрузки: замороженная граница, признак завершения и
// отказ, который не превращается в вечный SYNCING.

import { describe, expect, it, vi } from "vitest";

import { ApiProblem } from "../../api/problems";
import type { ChatMessage } from "../../shared/lib/types";
import { advance, requestFor, runSync, startSync, type SyncPageResult } from "./sync";

function page(overrides: Partial<SyncPageResult> = {}): SyncPageResult {
  return { items: [], hasMore: false, nextAfterSeq: null, syncToSeq: null, ...overrides };
}

function message(seq: number): ChatMessage {
  return { id: `m-${seq}`, seq, authorId: "u-1", kind: "text", text: `#${seq}`, timestamp: "10:00" };
}

describe("курсор догрузки", () => {
  it("первый запрос идёт без through_seq — он и замораживает границу", () => {
    expect(requestFor(startSync(100))).toEqual({ afterSeq: 100 });
  });

  it("продолжение несёт границу первого ответа", () => {
    const first = advance(startSync(100), page({ hasMore: true, nextAfterSeq: 150, syncToSeq: 200 }));
    if (first.kind !== "continue") throw new Error(`ожидалось продолжение, получено ${first.kind}`);

    expect(requestFor(first.cursor)).toEqual({ afterSeq: 150, throughSeq: 200 });
  });

  it("граница не пересчитывается: новый sync_to_seq игнорируется", () => {
    // Иначе догрузка уезжает за голову, и сходимость недостижима: каждая
    // страница отодвигала бы границу дальше.
    const first = advance(startSync(100), page({ hasMore: true, nextAfterSeq: 150, syncToSeq: 200 }));
    if (first.kind !== "continue") throw new Error("ожидалось продолжение");

    const second = advance(
      first.cursor,
      page({ hasMore: true, nextAfterSeq: 250, syncToSeq: 999 }),
    );
    if (second.kind !== "continue") throw new Error("ожидалось продолжение");

    expect(second.cursor.frozenThrough).toBe(200);
    expect(requestFor(second.cursor).throughSeq).toBe(200);
  });
});

describe("признак завершения", () => {
  it("has_more false и next_after_seq null — граница достигнута", () => {
    expect(advance(startSync(100), page())).toEqual({ kind: "done" });
  });

  it("пустая страница законна и не считается отказом", () => {
    // `through_seq == after_seq` — так выглядит повтор запроса, который клиент
    // уже выполнил. Ответить ошибкой значило бы наказать за повтор, а повтор
    // после обрыва связи — обычное дело.
    const step = advance(startSync(100), page({ items: [], hasMore: false, nextAfterSeq: null }));

    expect(step.kind).toBe("done");
  });

  it("пустая страница не завершает, пока есть курсор", () => {
    const step = advance(startSync(100), page({ hasMore: true, nextAfterSeq: 150 }));

    expect(step.kind).toBe("continue");
  });

  it("has_more без курсора — несогласованный ответ, а не завершение", () => {
    const step = advance(startSync(100), page({ hasMore: true, nextAfterSeq: null }));

    expect(step).toEqual({ kind: "stuck", reason: "no-cursor" });
  });

  it("курсор, не продвинувшийся вперёд, останавливает цикл", () => {
    const step = advance(startSync(100), page({ hasMore: true, nextAfterSeq: 100 }));

    expect(step).toEqual({ kind: "stuck", reason: "no-progress" });
  });
});

describe("цикл догрузки", () => {
  it("идёт до границы и называет число страниц", async () => {
    const loadPage = vi
      .fn()
      .mockResolvedValueOnce(page({ items: [message(101)], hasMore: true, nextAfterSeq: 101, syncToSeq: 102 }))
      .mockResolvedValueOnce(page({ items: [message(102)], hasMore: false, nextAfterSeq: null }));
    const seen: SyncPageResult[] = [];

    const outcome = await runSync({ loadPage, from: 100, onPage: (r) => seen.push(r) });

    expect(outcome).toEqual({ kind: "done", pages: 2 });
    expect(seen).toHaveLength(2);
    expect(loadPage.mock.calls[1][0]).toEqual({ afterSeq: 101, throughSeq: 102 });
  });

  it("страницы применяются по мере прихода, а не в конце", async () => {
    const applied: number[] = [];
    const loadPage = vi
      .fn()
      .mockResolvedValueOnce(page({ items: [message(101)], hasMore: true, nextAfterSeq: 101, syncToSeq: 102 }))
      .mockResolvedValueOnce(page({ items: [message(102)], hasMore: false, nextAfterSeq: null }));

    await runSync({ loadPage, from: 100, onPage: (r) => applied.push(...r.items.map((m) => m.seq)) });

    expect(applied).toEqual([101, 102]);
  });

  it("отказ 400 пробрасывается, а не выдаётся за завершение", async () => {
    // `400` здесь — дефект клиента (оба курсора сразу, `through_seq` меньше
    // `after_seq`): состояние не должно стать «синхронизировано», а связь —
    // «разорвана». Ошибка обязана быть видна.
    const loadPage = vi.fn().mockRejectedValue(new ApiProblem({ status: 400, title: "Bad Request" }));

    await expect(runSync({ loadPage, from: 100 })).rejects.toBeInstanceOf(ApiProblem);
  });

  it("курсор, вставший на месте, не зацикливает догрузку", async () => {
    const loadPage = vi.fn().mockResolvedValue(page({ hasMore: true, nextAfterSeq: 100 }));

    const outcome = await runSync({ loadPage, from: 100 });

    expect(outcome).toEqual({ kind: "stuck", reason: "no-progress", pages: 1 });
    expect(loadPage).toHaveBeenCalledTimes(1);
  });
});
