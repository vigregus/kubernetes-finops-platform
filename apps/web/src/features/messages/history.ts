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
import type { ListMessages200Response, ListMessagesRequest } from "../../api/generated";
import type { ChatMessage } from "../../shared/lib/types";
import type { TimestampFormatOptions } from "../conversations/formatTimestamp";
import { applySnapshot, type MergeOutcome, type MergeState } from "./eventMerge";
import { adaptMessages } from "./message-adapter";
import type { SyncPage, SyncPageResult } from "./sync";

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

/**
 * Ответ `GET /messages` → страница хвоста.
 *
 * Границы ответственности названы прямо, потому что здесь они встречаются:
 * **форма ответа — этого модуля**, а перевод одной записи `Message` →
 * `ChatMessage` — `message-adapter.ts`. Разделение не косметическое: форма
 * страницы (сколько элементов, есть ли продолжение, какая граница) читается
 * один раз и только тут, а записей в странице пятьдесят, и каждая проходит
 * общую таблицу соответствия. Свернуть это в `items.map(adaptMessage)` на месте
 * вызова значило бы, что форма ответа собирается там, где её читают, — а её
 * читает ещё и продолжение догрузки, и собирать её дважды незачем.
 *
 * `nextBeforeSeq` переносится в модель, но **не используется**: за курсором
 * вглубь в объёме G3-006 не идём, и делать вид, что страница — вся беседа,
 * интерфейс не должен. Поле здесь затем, чтобы «не используется» было видно в
 * типе, а не выводилось из того, что его нигде нет.
 */
export function tailPageOf(
  response: ListMessages200Response,
  currentUserId: string,
  now: Date,
  timestampOptions: TimestampFormatOptions = {},
): TailPage {
  return {
    items: adaptMessages(response.items, currentUserId, now, timestampOptions),
    hasMore: response.hasMore,
    nextBeforeSeq: response.nextBeforeSeq,
    syncToSeq: response.syncToSeq,
  };
}

/**
 * Чем история читает сервер. Операция **одна**, и это не совпадение: обе дороги
 * — хвост без курсора и догрузка с `after_seq` — это один `listMessages` с
 * разными параметрами. Разводить их по двум клиентам значило бы завести две
 * копии формы ответа, которые однажды разойдутся.
 */
export interface HistoryApi {
  listMessages(request: ListMessagesRequest): Promise<ListMessages200Response>;
}

/**
 * Две дороги истории в том виде, в каком их ждёт `useConversationHistory`.
 *
 * Загрузчики, а не состояние: этот модуль знает **форму ответа**, но не знает,
 * ни когда спрашивать, ни что делать с пропуском. Такое разделение позволяет
 * проверять протокол догрузки прогоном без сети (`sync.ts`), а форму ответа —
 * прогоном без хука (здесь).
 */
export interface HistorySource {
  readonly loadTail: (conversationId: string) => Promise<TailPage>;
  readonly loadPage: (conversationId: string, page: SyncPage) => Promise<SyncPageResult>;
}

export interface HistorySourceOptions {
  readonly api: HistoryApi;
  /** `"me"` в модели — это взгляд; идентификатор домена приходит отсюда. */
  readonly currentUserId: string;
  /**
   * Момент, относительно которого читается display-время.
   *
   * Функция, а не значение: страницы читаются в разные мгновения, и общий
   * замороженный `Date` на всю жизнь соединения показывал бы «вчера» там, где
   * уже «сегодня». В тестах это же позволяет зафиксировать время.
   */
  readonly now?: () => Date;
  readonly timestampOptions?: TimestampFormatOptions;
}

/**
 * Загрузчики истории поверх клиента API.
 *
 * Границы ответственности здесь видны в сигнатурах, и это главное, что этот
 * шов делает: `conversationId` **обязан доехать до запроса** — он приходит
 * вызывающим, а не берётся из замыкания, потому что одна и та же фабрика
 * обслуживает любую открытую беседу, и подстановка «текущей» внутри неё
 * привязала бы ответ к беседе, которой в запросе нет.
 *
 * `throughSeq` догрузки переносится **как есть**, включая `undefined`: на
 * первом запросе границы ещё нет, и появление здесь любого числа означало бы
 * пересчёт границы вместо её заморозки (B14).
 */
export function createHistorySource(options: HistorySourceOptions): HistorySource {
  const { api, currentUserId, now = () => new Date(), timestampOptions = {} } = options;

  return {
    loadTail: async (conversationId) => {
      const response = await api.listMessages({ conversationId, ...tailRequest() });

      return tailPageOf(response, currentUserId, now(), timestampOptions);
    },

    loadPage: async (conversationId, page) => {
      const response = await api.listMessages({
        conversationId,
        afterSeq: page.afterSeq,
        throughSeq: page.throughSeq,
        limit: TAIL_LIMIT,
      });

      return {
        items: adaptMessages(response.items, currentUserId, now(), timestampOptions),
        hasMore: response.hasMore,
        nextAfterSeq: response.nextAfterSeq,
        // Без этого поля курсор не заморозится никогда: `advance` оставит
        // границу `null`, и следующий запрос пересчитает её заново.
        syncToSeq: response.syncToSeq,
      };
    },
  };
}
