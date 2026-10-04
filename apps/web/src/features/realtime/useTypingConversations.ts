/**
 * Кто печатает **в каких беседах** — состояние получателя поверх `typingState`.
 *
 * Состояние одно на страницу, а не на панель беседы: «печатает» нужно и в
 * шапке открытой беседы, и в строке списка у беседы, которая сейчас не открыта.
 * Публикации приходят по каналу `typing:{conversation_id}` — сервер подписывает
 * соединение на канал набора каждой беседы тикета, — и разбираются здесь по
 * имени канала. Запись живёт до своего срока и гаснет сама; таймер заводится
 * ровно на ближайшее истечение, а не тикает постоянно.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  activeUsers,
  applyTyping,
  nextExpiry,
  parseTypingEvent,
  prune,
  type TypingMap,
} from "./typingState";

const PREFIX = "typing:";

export type TypingByConversation = ReadonlyMap<string, TypingMap>;

const EMPTY: TypingByConversation = new Map();

function earliestExpiry(state: TypingByConversation): number | null {
  let soonest: number | null = null;
  for (const map of state.values()) {
    const next = nextExpiry(map);
    if (next !== null && (soonest === null || next < soonest)) soonest = next;
  }
  return soonest;
}

export interface TypingConversations {
  /** Беседа → идентификаторы тех, кто печатает прямо сейчас (без самого человека). */
  readonly byConversation: ReadonlyMap<string, readonly string[]>;
  /** Обработчик публикации канала набора — в `useRealtimeConnection`. */
  readonly onPublication: (channel: string, payload: unknown) => void;
}

export function useTypingConversations(selfId: string): TypingConversations {
  const [state, setState] = useState<TypingByConversation>(EMPTY);
  // Время хранится состоянием, а не берётся из часов в рендере: рендер остаётся
  // чистым, а перерисовку будят событие и таймер.
  const [now, setNow] = useState(() => Date.now());

  const onPublication = useCallback(
    (channel: string, payload: unknown) => {
      if (!channel.startsWith(PREFIX)) return;
      const conversationId = channel.slice(PREFIX.length);
      if (conversationId === "") return;
      const event = parseTypingEvent(payload);
      if (event === null) return;
      const at = Date.now();
      setState((current) => {
        const before = current.get(conversationId) ?? new Map<string, number>();
        const after = applyTyping(before, event, at, selfId);
        if (after === before) return current;
        const next = new Map(current);
        if (after.size === 0) next.delete(conversationId);
        else next.set(conversationId, after);
        return next;
      });
      setNow(at);
    },
    [selfId],
  );

  useEffect(() => {
    const expiry = earliestExpiry(state);
    if (expiry === null) return;
    const timer = setTimeout(() => {
      const at = Date.now();
      setState((current) => {
        const next = new Map<string, TypingMap>();
        for (const [conversationId, map] of current) {
          const alive = prune(map, at);
          if (alive.size > 0) next.set(conversationId, alive);
        }
        return next;
      });
      setNow(at);
    }, Math.max(0, expiry - Date.now()) + 5);
    return () => clearTimeout(timer);
  }, [state]);

  const byConversation = useMemo(() => {
    const out = new Map<string, readonly string[]>();
    for (const [conversationId, map] of state) {
      const users = activeUsers(map, now);
      if (users.length > 0) out.set(conversationId, users);
    }
    return out;
  }, [state, now]);

  return { byConversation, onPublication };
}
