/**
 * Соединение **списка бесед** — когда панели беседы нет.
 *
 * На телефоне экран списка не монтирует панель беседы (иначе первая беседа списка
 * грузила бы историю и отмечала прочитанным то, чего человек не открывал). Но
 * соединение, которое слушает личный канал `user:{id}` (`unread.changed`) и наборы
 * `typing:*`, живёт в панели беседы (`useRealtimeConnection`): без неё список на
 * телефоне — снимок на момент загрузки, и новое сообщение его не меняет.
 *
 * Эта обвязка держит ровно то, что нужно списку, и ничего из того, что нужно беседе:
 * билет тот же и выдаёт те же каналы (подписки серверные), но публикации ни одной
 * беседы сюда не доходят, а истории, квитанций и автомата соединения нет. Пока
 * открыта беседа, соединение у панели; здесь оно **выключено** (`enabled`), чтобы не
 * держать два.
 *
 * Каждое подключение (в том числе после обрыва и после возврата из беседы) зовёт
 * `onConnected`: за время, пока соединения не было, события могли пройти мимо, и список
 * перечитывается (истина — REST, событие лишь ускоряет).
 */
import { useEffect, useRef } from "react";

import type { RealtimeClientOptions } from "./realtimeClient";
import { createRealtimeClient } from "./realtimeClient";

export interface UseListRealtimeOptions {
  readonly enabled: boolean;
  readonly centrifugoUrl: string;
  /** `user:{id}` — личный канал. */
  readonly userChannel: string;
  readonly issueTicket: RealtimeClientOptions["issueTicket"];
  readonly onUserPublication: (payload: unknown) => void;
  readonly onTypingPublication?: (channel: string, payload: unknown) => void;
  /** Соединение поднято: пропущенное перечитать. */
  readonly onConnected: () => void;
  readonly createCentrifuge?: RealtimeClientOptions["createCentrifuge"];
}

export function useListRealtime(options: UseListRealtimeOptions): void {
  const userRef = useRef(options.onUserPublication);
  const typingRef = useRef(options.onTypingPublication);
  const connectedRef = useRef(options.onConnected);
  useEffect(() => {
    userRef.current = options.onUserPublication;
    typingRef.current = options.onTypingPublication;
    connectedRef.current = options.onConnected;
  }, [options.onUserPublication, options.onTypingPublication, options.onConnected]);

  const { enabled, centrifugoUrl, userChannel, issueTicket, createCentrifuge } = options;

  useEffect(() => {
    if (!enabled) return;

    const client = createRealtimeClient({
      centrifugoUrl,
      userChannel,
      issueTicket,
      onEvent: (event) => {
        if (event.type === "sdk-connected") connectedRef.current();
      },
      onUserPublication: (payload) => userRef.current(payload),
      onTypingPublication: (channel, payload) => typingRef.current?.(channel, payload),
      createCentrifuge,
    });
    client.start();

    // SDK после потери сети сам не возобновляет попытки (см. `RealtimeClient.connect`):
    // возобновляем по факту браузера.
    const resume = () => client.connect();
    window.addEventListener("online", resume);

    return () => {
      window.removeEventListener("online", resume);
      client.stop();
    };
  }, [enabled, centrifugoUrl, userChannel, issueTicket, createCentrifuge]);
}
