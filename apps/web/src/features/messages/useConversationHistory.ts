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
 * правды о состоянии.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatMessage } from "../../shared/lib/types";
import { createBootstrapBuffer } from "./bootstrapBuffer";
import {
  applyBuffered,
  applyMessage,
  applyPage,
  emptyMergeState,
  type MergeState,
} from "./eventMerge";
import { applyTail, type TailPage } from "./history";
import { runSync, type SyncPage, type SyncPageResult } from "./sync";

/** Хвост ещё не применён · применён · не доехал. */
export type HistoryPhase = "loading" | "ready" | "error";

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
}

export interface ConversationHistory {
  readonly messages: readonly ChatMessage[];
  /** `null` — граница неизвестна: снимок ещё не применён. */
  readonly appliedThroughSeq: number | null;
  readonly phase: HistoryPhase;
  /** Публикация канала: до снимка — в буфер, после — в ленту. */
  acceptPublication(message: ChatMessage): void;
  /** Догрузка от применённой границы до замороженной. */
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
        let next = tail.state;

        const buffered = bufferRef.current.drain();
        if (bufferRef.current.overflowed) {
          // Буфер снял с себя работу: содержимое вернёт догрузка от границы.
          handlersRef.current.onGapDetected?.();
        } else if (buffered.length > 0) {
          const replay = applyBuffered(next, buffered);
          next = replay.state;
          if (replay.kind === "gap") {
            handlersRef.current.onGapDetected?.();
          }
        }

        loadedRef.current = true;
        commit(next);
        setPhase("ready");
      } catch {
        if (!cancelled) setPhase("error");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [conversationId, commit]);

  const acceptPublication = useCallback(
    (message: ChatMessage) => {
      if (!loadedRef.current) {
        bufferRef.current.add(message);
        return;
      }
      const outcome = applyMessage(stateRef.current, message);
      commit(outcome.state);
      if (outcome.kind === "gap") {
        handlersRef.current.onGapDetected?.();
      }
    },
    [commit],
  );

  const startSync = useCallback(async () => {
    const from = stateRef.current.appliedThroughSeq;
    if (from === null) {
      // Снимка ещё нет: догружать не от чего. Пропуск в это окно закрывает
      // буфер, а не синхронизация.
      return;
    }
    await runSync({
      from,
      loadPage: (page) => handlersRef.current.loadPage(conversationId, page),
      onPage: (result) => {
        commit(applyPage(stateRef.current, result.items).state);
      },
    });
  }, [commit, conversationId]);

  return {
    messages: state.messages,
    appliedThroughSeq: state.appliedThroughSeq,
    phase,
    acceptPublication,
    startSync,
  };
}
