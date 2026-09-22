/**
 * Первый хвост беседы — одна страница `listMessages` без курсора.
 *
 * Глубже одной страницы не идём: бесконечной прокрутки в объёме G3-006 нет.
 * Одна страница — это то, что делает применённую границу честной, а ленту —
 * наблюдаемой.
 *
 * Две ловушки контракта, обе измеренные:
 *
 * * **Хвост приходит по убыванию.** `openapi.yaml:218-221` и
 *   `repositories/messages.py:263-275`: листание назад отдаёт новые первыми,
 *   потому что так их показывает интерфейс. Значит голова — `max(seq)`, а не
 *   последний элемент массива. Взятая из последнего, она была бы равна 101 при
 *   голове 150, и догрузка поехала бы не оттуда.
 * * **`sync_to_seq` у листания не читается вовсе.** Он `null`
 *   (`openapi.yaml:319-321`): «листание не синхронизация, и границы у него
 *   нет». Граница заводится только запросом с `after_seq` без `through_seq`.
 */
import type { ChatMessage } from "../../shared/lib/types";
import { applySnapshot, type MergeOutcome, type MergeState } from "./eventMerge";

/** Сколько сообщений тянем хвостом. Значение названо, а не «на глаз». */
export const TAIL_LIMIT = 50;

/** Ответ хвоста — та часть, которая входит в модель. */
export interface TailPage {
  readonly items: readonly ChatMessage[];
  readonly hasMore: boolean;
  readonly nextBeforeSeq?: number | null;
  /** Приходит `null`; поле объявлено, чтобы «не читается» было видно в типе. */
  readonly syncToSeq?: number | null;
}

/** Запрос хвоста: без курсоров — листание назад от головы. */
export function tailRequest(limit: number = TAIL_LIMIT): { readonly limit: number } {
  return { limit };
}

/**
 * Хвост входит в модель снимком: граница — максимальный номер страницы.
 *
 * `syncToSeq` намеренно не участвует даже тогда, когда сервер его прислал:
 * граница догрузки берётся из ответа **синхронизации**, и подставлять сюда
 * значение из листания значило бы заморозить снимок числом, смысла которого
 * мы не знаем.
 */
export function applyTail(state: MergeState, page: TailPage): MergeOutcome {
  return applySnapshot(state, page.items);
}
