/**
 * Отправка «печатает»: первое нажатие — сразу, дальше сердцебиение не чаще
 * раза в `HEARTBEAT_MS`, очистка поля и отправка — `stop`.
 *
 * Клиент сам не заливает канал: даже если человек печатает быстро, публикаций
 * не больше одной за период. Сервер при этом всё равно держит лимит
 * (`RT-002`), потому что клиенту верить нельзя, — но честный клиент до него
 * не дорастает.
 *
 * Время и отправка приходят параметрами: сценарии (быстрая печать, пауза,
 * очистка) предъявляются без таймеров и сети.
 */
export const HEARTBEAT_MS = 2500;

export type TypingSignal = "typing" | "stop";

export interface TypingSender {
  /** Поле композера изменилось: `hasText` — есть ли в нём что отправлять. */
  input(hasText: boolean): void;
  /** Закончить набор принудительно: отправили сообщение, ушли из беседы. */
  stop(): void;
}

export interface TypingSenderOptions {
  readonly send: (signal: TypingSignal) => void;
  readonly now: () => number;
  readonly heartbeatMs?: number;
}

export function createTypingSender(options: TypingSenderOptions): TypingSender {
  const heartbeat = options.heartbeatMs ?? HEARTBEAT_MS;
  let typing = false;
  let lastSentAt = 0;

  const stop = () => {
    if (!typing) return;
    typing = false;
    options.send("stop");
  };

  return {
    input(hasText) {
      if (!hasText) {
        stop();
        return;
      }
      const now = options.now();
      if (!typing || now - lastSentAt >= heartbeat) {
        typing = true;
        lastSentAt = now;
        options.send("typing");
      }
    },
    stop,
  };
}
