/**
 * Протокол догрузки пропущенного — `after_seq` / `through_seq` / `has_more`.
 *
 * Модуль владеет **курсором** и ничего не знает ни о ленте, ни о сети: страницу
 * применяет вызывающий (`eventMerge`), а саму страницу приносит `loadPage`.
 * Поэтому каждая крайность протокола проверяется прогоном без сети.
 *
 * Почему граница замораживается. Голова беседы движется: пока клиент догружает
 * 101–150, приходят 201–300, и часть из них уже несёт поток. Догонять
 * движущуюся голову по REST и одновременно принимать те же номера по
 * WebSocket — гонка, которую клиент выиграть не может: он не знает, где
 * кончается снимок и начинается поток. Первый запрос **без** `through_seq`
 * замораживает границу (`sync_to_seq` ответа), все следующие идут с ней
 * (`openapi.yaml:201-320`).
 */
import type { ChatMessage } from "../../shared/lib/types";

/** Параметры запроса страницы. `through_seq` появляется только со второго. */
export interface SyncPage {
  readonly afterSeq: number;
  readonly throughSeq?: number;
}

/** Ответ сервера — та его часть, которая определяет продолжение. */
export interface SyncPageResult {
  readonly items: readonly ChatMessage[];
  readonly hasMore: boolean;
  readonly nextAfterSeq?: number | null;
  readonly syncToSeq?: number | null;
}

export interface SyncCursor {
  readonly afterSeq: number;
  /** Замороженная граница снимка: `sync_to_seq` **первого** ответа. */
  readonly frozenThrough: number | null;
}

/**
 * Почему цикл остановлен, не дойдя до границы.
 *
 * Оба случая — дефект, а не состояние связи: сервер, отвечающий так, нарушает
 * контракт. Молчаливое «остановились» выдало бы это за сходимость.
 */
export type SyncStuckReason =
  /** `has_more` без `next_after_seq`: продолжать нечем. */
  | "no-cursor"
  /** `next_after_seq` не продвинулся: продолжение вернуло бы ту же страницу. */
  | "no-progress";

export type SyncStep =
  | { readonly kind: "continue"; readonly cursor: SyncCursor }
  | { readonly kind: "done" }
  | { readonly kind: "stuck"; readonly reason: SyncStuckReason };

/** Первый курсор — от применённой границы, без замороженной. */
export function startSync(appliedThroughSeq: number): SyncCursor {
  return { afterSeq: appliedThroughSeq, frozenThrough: null };
}

export function requestFor(cursor: SyncCursor): SyncPage {
  return cursor.frozenThrough === null
    ? { afterSeq: cursor.afterSeq }
    : { afterSeq: cursor.afterSeq, throughSeq: cursor.frozenThrough };
}

/**
 * Что делать после страницы.
 *
 * Завершение читается **парой** `has_more === false` **и**
 * `next_after_seq === null`, а не пустотой `items`: пустая страница законна —
 * так выглядит повтор уже выполненного запроса (`through_seq == after_seq`), и
 * принять её за «догнали» значило бы либо объявить сходимость там, где её нет,
 * либо, наоборот, отказать в законном `200`.
 */
export function advance(cursor: SyncCursor, result: SyncPageResult): SyncStep {
  const frozenThrough = cursor.frozenThrough ?? result.syncToSeq ?? null;

  if (!result.hasMore && (result.nextAfterSeq ?? null) === null) {
    return { kind: "done" };
  }
  const next = result.nextAfterSeq ?? null;
  if (next === null) {
    return { kind: "stuck", reason: "no-cursor" };
  }
  if (next <= cursor.afterSeq) {
    return { kind: "stuck", reason: "no-progress" };
  }
  return { kind: "continue", cursor: { afterSeq: next, frozenThrough } };
}

export interface RunSyncOptions {
  readonly loadPage: (page: SyncPage) => Promise<SyncPageResult>;
  readonly from: number;
  /** Страница получена — вызывающий применяет её к ленте. */
  readonly onPage?: (result: SyncPageResult) => void;
  /** Предел страниц: защита от цикла, который не заметил бы даже `advance`. */
  readonly maxPages?: number;
}

export type SyncRunOutcome =
  | { readonly kind: "done"; readonly pages: number }
  | { readonly kind: "stuck"; readonly reason: SyncStuckReason; readonly pages: number };

/** Предел страниц — не «сколько бывает», а «дальше заведомо ошибка». */
export const MAX_SYNC_PAGES = 1000;

/**
 * Цикл догрузки до границы.
 *
 * Отказ `loadPage` **пробрасывается**, а не превращается в остановку: `400` —
 * это дефект клиента (`openapi.yaml:608-613`), и он обязан быть виден как
 * ошибка. Проглотив его, мы получили бы вечный `SYNCING` вместо сообщения о
 * том, что запрос построен неверно.
 */
export async function runSync(options: RunSyncOptions): Promise<SyncRunOutcome> {
  const limit = options.maxPages ?? MAX_SYNC_PAGES;
  let cursor = startSync(options.from);
  let pages = 0;

  while (pages < limit) {
    const result = await options.loadPage(requestFor(cursor));
    pages += 1;
    options.onPage?.(result);

    const step = advance(cursor, result);
    if (step.kind === "done") {
      return { kind: "done", pages };
    }
    if (step.kind === "stuck") {
      return { kind: "stuck", reason: step.reason, pages };
    }
    cursor = step.cursor;
  }

  return { kind: "stuck", reason: "no-progress", pages };
}
