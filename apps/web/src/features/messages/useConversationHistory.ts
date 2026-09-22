/**
 * Состояние ленты для интерфейса: сводит хвост, публикации канала и догрузку.
 *
 * Собственных решений у обвязки нет — они в `eventMerge`, `bootstrapBuffer`,
 * `sync` и `history`, и там же проверяются прогоном без сети. Здесь только
 * порядок шагов и время жизни: соединение поднимается **первым** (композиция),
 * публикации до готовности снимка копятся в буфере, снимок применяется
 * вторым, буфер проигрывается третьим.
 *
 * Обвязка не толкует состояния соединения: о пропуске она сообщает наружу
 * (`onGapDetected`), а перевод в `SYNCING` делает автомат — единственная точка
 * правды о состоянии. О сходимости — тоже наружу (`onSyncDone`), и это не
 * формальность: **промежуточная сходимость не объявляется**. Круг догрузки
 * ограничен замороженной границей, поток за это время уходит выше неё, и
 * «круг кончился» вовсе не значит «дыр нет» — значит это только проигранный
 * буфер.
 *
 * **Буфер поглощает публикации в двух окнах, а не в одном.** Пока снимка нет и
 * пока идёт догрузка — по одной и той же причине: номер применить некуда.
 * Второе окно неочевидно и стоило бы потери: публикация с номером выше
 * замороженной границы не применяется (для границы она — дыра) и догрузкой не
 * возвращается (`through_seq` ограничивает ответ). Её может вернуть только
 * буфер.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatMessage } from "../../shared/lib/types";
import { createBootstrapBuffer } from "./bootstrapBuffer";
import {
  applyMessage,
  applyPage,
  emptyMergeState,
  settleAfterRound,
  type MergeState,
} from "./eventMerge";
import { applyTail, type TailPage } from "./history";
import { runSync, type SyncPage, type SyncPageResult } from "./sync";

/** Хвост ещё не применён · применён · не доехал. */
export type HistoryPhase = "loading" | "ready" | "error";

/**
 * Предел кругов догрузки.
 *
 * Круг — это REST-проход до замороженной границы плюс проигрывание буфера.
 * Второй круг нужен, когда буфер вскрыл пропуск **выше** границы, и в норме
 * их один-два. Предел здесь потому, что непрерывный поток публикаций может
 * не дать сойтись никогда, и это должно быть названо, а не крутиться вечно.
 */
export const MAX_SYNC_ROUNDS = 100;

export interface ConversationHistoryOptions {
  readonly conversationId: string;
  /** Одна страница хвоста — листание назад, без курсоров. */
  readonly loadTail: (conversationId: string) => Promise<TailPage>;
  /** Страница догрузки — `after_seq` с замороженной границей. */
  readonly loadPage: (conversationId: string, page: SyncPage) => Promise<SyncPageResult>;
  /**
   * Обнаружен пропуск: часть беседы не дошла.
   *
   * Наружу, а не внутрь: состояние интерфейса ведёт автомат, и он же назовёт
   * причину (`sequence-gap`).
   */
  readonly onGapDetected?: () => void;
  /**
   * Догрузка сошлась: буфер проигран, номеров выше границы не осталось.
   *
   * Зовётся **один раз на сходимость**, а не на каждый круг: пока в буфере
   * остаётся пропуск, состояние обязано остаться `SYNCING`
   * (`03-v1-scope.md:196` — старое не выдаётся за актуальное).
   */
  readonly onSyncDone?: () => void;
  /**
   * Догрузка отказала — `400` и подобное.
   *
   * Отказ обязан быть виден: это дефект клиента (оба курсора сразу,
   * `through_seq` меньше `after_seq` — `openapi.yaml:608-613`), и, проглотив
   * его, мы получили бы вечный `SYNCING` вместо сообщения об ошибке.
   */
  readonly onSyncFailed?: (error: unknown) => void;
}

export interface ConversationHistory {
  readonly messages: readonly ChatMessage[];
  /** `null` — граница неизвестна: снимок ещё не применён. */
  readonly appliedThroughSeq: number | null;
  readonly phase: HistoryPhase;
  /** Публикация канала: до снимка и на время догрузки — в буфер, иначе в ленту. */
  acceptPublication(message: ChatMessage): void;
  /** Догрузка от применённой границы до замороженной. Намерение не теряется. */
  startSync(): Promise<void>;
}

export function useConversationHistory(
  options: ConversationHistoryOptions,
): ConversationHistory {
  const [state, setState] = useState<MergeState>(emptyMergeState);
  const [phase, setPhase] = useState<HistoryPhase>("loading");

  // Состояние живёт в ссылке, а в `useState` дублируется для рендера: одна и та
  // же публикация и снимок могут прийти в одном тике, и читать `state` из
  // замыкания значило бы применить второе поверх первого.
  const stateRef = useRef<MergeState>(state);
  const bufferRef = useRef(createBootstrapBuffer());
  const loadedRef = useRef(false);
  /** Идёт догрузка: публикации в это окно применять нельзя, их поглотит буфер. */
  const syncingRef = useRef(false);
  /**
   * Догрузку просили, но границы ещё нет.
   *
   * Молча выйти здесь — не «нечего догружать», а **потеря намерения**:
   * расхождение, обнаруженное до готовности снимка, так и осталось бы
   * незамеченным. Снимок исполнит отложенное.
   */
  const pendingSyncRef = useRef(false);

  // Обработчики — в ссылке, но обновляется она **в эффекте**, а не в теле
  // рендера: запись в `ref` во время рендера React считает обращением к ref
  // при отрисовке, и это справедливо — рендер обязан быть чистым. Ссылка нужна
  // затем, чтобы эффект не перезапускал снимок из-за того, что композиция
  // пересоздала колбэк.
  const handlersRef = useRef(options);
  useEffect(() => {
    handlersRef.current = options;
  });

  const commit = useCallback((next: MergeState) => {
    stateRef.current = next;
    setState(next);
  }, []);

  const { conversationId } = options;

  /**
   * Разбор накопленного: буфер, и только если его не хватило — круг REST.
   *
   * Порядок именно такой. Публикация с номером ровно на следующем месте
   * закрывается буфером без всякого запроса; запрос нужен лишь тогда, когда
   * буфер вскрыл пропуск, которого у него нет, — и тогда круг идёт **от
   * текущей границы**, а не от той, с которой начинали.
   */
  const pump = useCallback(async () => {
    if (syncingRef.current) {
      return;
    }
    if (
      stateRef.current.appliedThroughSeq !== null &&
      !pendingSyncRef.current &&
      bufferRef.current.size === 0 &&
      !bufferRef.current.overflowed
    ) {
      // Разбирать нечего: ни намерения, ни накопленного. Выход здесь — не
      // «сходимость»: объявлять её без круга значило бы отчитываться о работе,
      // которой не было.
      return;
    }
    syncingRef.current = true;
    try {
      for (let round = 0; round < MAX_SYNC_ROUNDS; round += 1) {
        if (stateRef.current.appliedThroughSeq === null) {
          pendingSyncRef.current = true;
          return;
        }

        const replayed = settleAfterRound(
          stateRef.current,
          bufferRef.current.drain(),
          bufferRef.current.overflowed,
        );
        commit(replayed.state);

        if (replayed.kind === "settled" && !pendingSyncRef.current) {
          handlersRef.current.onSyncDone?.();
          return;
        }
        if (replayed.kind === "needs-another-round" && replayed.reason === "overflow") {
          // Содержимое отброшено намеренно: его вернёт следующий круг от
          // границы. Оставить признак было бы нечем снять — он липкий.
          bufferRef.current = createBootstrapBuffer();
        }
        if (replayed.kind === "before-snapshot") {
          pendingSyncRef.current = true;
          return;
        }

        const from = stateRef.current.appliedThroughSeq;
        try {
          await runSync({
            from,
            loadPage: (page) => handlersRef.current.loadPage(conversationId, page),
            onPage: (result) => {
              commit(applyPage(stateRef.current, result.items).state);
            },
          });
        } catch (error) {
          handlersRef.current.onSyncFailed?.(error);
          return;
        }
        pendingSyncRef.current = false;
      }

      // Круги исчерпаны, а пропуски остались: состояние остаётся `SYNCING`,
      // и это названо — молчаливая сходимость здесь была бы ложью.
      handlersRef.current.onGapDetected?.();
    } finally {
      syncingRef.current = false;
    }
  }, [commit, conversationId]);

  // Смена беседы сбрасывает состояние **ремоунтом** компонента (композиция
  // ставит `key={conversationId}`), а не сбросом внутри эффекта: сброс был бы
  // синхронным `setState` в эффекте — то есть вторым рендером на пустом месте,
  // при том что начальные значения и так верны.
  useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const page = await handlersRef.current.loadTail(conversationId);
        if (cancelled) return;

        const tail = applyTail(emptyMergeState(), page);
        loadedRef.current = true;
        commit(tail.state);
        setPhase("ready");

        // Накопленное до снимка и отложенное намерение разбираются **той же**
        // дорогой, что и всё остальное, — второй рядом нет.
        if (
          pendingSyncRef.current ||
          bufferRef.current.size > 0 ||
          bufferRef.current.overflowed
        ) {
          void pump();
        }
      } catch {
        if (!cancelled) setPhase("error");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [conversationId, commit, pump]);

  const acceptPublication = useCallback(
    (message: ChatMessage) => {
      if (!loadedRef.current || syncingRef.current) {
        bufferRef.current.add(message);
        return;
      }
      const outcome = applyMessage(stateRef.current, message);
      commit(outcome.state);
      if (outcome.kind === "gap") {
        pendingSyncRef.current = true;
        handlersRef.current.onGapDetected?.();
        void pump();
      }
    },
    [commit, pump],
  );

  const startSync = useCallback(async () => {
    // Намерение ставится **до** выхода в `pump`: иначе вызов, пришедший до
    // готовности снимка, не оставил бы следа, и расхождение потерялось бы.
    pendingSyncRef.current = true;
    await pump();
  }, [pump]);

  return {
    messages: state.messages,
    appliedThroughSeq: state.appliedThroughSeq,
    phase,
    acceptPublication,
    startSync,
  };
}
