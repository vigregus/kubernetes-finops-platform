/**
 * Кто печатает в этой беседе — состояние получателя поверх `typingState`.
 *
 * Публикации приходят из канала `typing:{id}`; запись живёт до своего срока и
 * гаснет сама. Таймер заводится ровно на ближайшее истечение, а не тикает
 * постоянно: пока никто не печатает, интерфейс не перерисовывается.
 */
import { useCallback, useEffect, useState } from "react";

import {
  activeUsers,
  applyTyping,
  EMPTY_TYPING,
  nextExpiry,
  parseTypingEvent,
  prune,
  type TypingMap,
} from "./typingState";

export interface TypingPeers {
  /** Идентификаторы тех, кто печатает прямо сейчас (без самого человека). */
  readonly userIds: readonly string[];
  /** Обработчик публикации канала набора — в `useRealtimeConnection`. */
  readonly onPublication: (payload: unknown) => void;
}

export function useTypingPeers(selfId: string): TypingPeers {
  const [state, setState] = useState<TypingMap>(EMPTY_TYPING);
  // Время для расчёта «кто активен» хранится состоянием, а не берётся из часов
  // в рендере: рендер остаётся чистым, а перерисовку будят событие и таймер.
  const [now, setNow] = useState(() => Date.now());

  const onPublication = useCallback(
    (payload: unknown) => {
      const event = parseTypingEvent(payload);
      if (event === null) return;
      const at = Date.now();
      setState((current) => applyTyping(current, event, at, selfId));
      setNow(at);
    },
    [selfId],
  );

  useEffect(() => {
    const expiry = nextExpiry(state);
    if (expiry === null) return;
    const timer = setTimeout(() => {
      const at = Date.now();
      setState((current) => prune(current, at));
      setNow(at);
    }, Math.max(0, expiry - Date.now()) + 5);
    return () => clearTimeout(timer);
  }, [state]);

  return { userIds: activeUsers(state, now), onPublication };
}
