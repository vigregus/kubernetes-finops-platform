/**
 * Кто сейчас печатает: состояние держит **получатель**, а не сервер.
 *
 * Каждое сердцебиение продлевает срок только своего пользователя, а `stop` —
 * не обязательность, а ускорение: закрытая вкладка его не пришлёт, и индикатор
 * обязан погаснуть сам (`RT-001`). Поэтому состояние — не флаг, а
 * «пользователь → момент истечения», и всё, что истекло, просто перестаёт
 * считаться. Функции чистые: время приходит параметром.
 */

/** Событие из `typing:{conversation_id}` как оно разобрано (`channels.json`, `typingEvent`). */
export interface TypingEvent {
  readonly userId: string;
  readonly expiresInMs: number;
}

/** Пользователь → момент истечения, мс на часах вызывающего. */
export type TypingMap = ReadonlyMap<string, number>;

export const EMPTY_TYPING: TypingMap = new Map();

/**
 * Публикация приходит из сети, поэтому разбор строгий: чужая форма отбрасывается
 * целиком, а не угадывается. `user_id` берётся из тела **как факт сервера** —
 * тело подменено сервером из аутентифицированного соединения (publish-proxy),
 * клиентская версия до получателей не доходит.
 */
export function parseTypingEvent(payload: unknown): TypingEvent | null {
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  const userId = record["user_id"];
  const expires = record["expires_in_ms"];
  if (typeof userId !== "string" || userId === "") return null;
  if (typeof expires !== "number" || !Number.isFinite(expires) || expires < 0) return null;
  return { userId, expiresInMs: expires };
}

/**
 * Применить событие. Своё собственное (набор в другой вкладке того же человека)
 * не показывается: человек не видит «вы печатаете». Срок ноль — `stop`, запись
 * снимается сразу.
 */
export function applyTyping(
  state: TypingMap,
  event: TypingEvent,
  nowMs: number,
  selfId: string,
): TypingMap {
  if (event.userId === selfId) return state;
  const next = new Map(state);
  if (event.expiresInMs <= 0) next.delete(event.userId);
  else next.set(event.userId, nowMs + event.expiresInMs);
  return next;
}

/** Кто печатает прямо сейчас: истёкшее не считается, а не удаляется молча. */
export function activeUsers(state: TypingMap, nowMs: number): string[] {
  return [...state].filter(([, expiresAt]) => expiresAt > nowMs).map(([userId]) => userId);
}

/** Убрать истёкшее — чтобы карта не росла от каждого, кто когда-то печатал. */
export function prune(state: TypingMap, nowMs: number): TypingMap {
  const alive = [...state].filter(([, expiresAt]) => expiresAt > nowMs);
  return alive.length === state.size ? state : new Map(alive);
}

/** Ближайший момент, когда что-то истечёт, — чтобы разбудить интерфейс ровно тогда. */
export function nextExpiry(state: TypingMap): number | null {
  let soonest: number | null = null;
  for (const expiresAt of state.values()) {
    if (soonest === null || expiresAt < soonest) soonest = expiresAt;
  }
  return soonest;
}
