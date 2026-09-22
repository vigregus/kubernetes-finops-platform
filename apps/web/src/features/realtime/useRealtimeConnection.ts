/**
 * React-обвязка: два источника фактов сводятся в один автомат.
 *
 * Источника ровно два, и они разной природы. SDK говорит о сокете; браузер
 * (`offline`/`online`) — о сети, и его факт **старше**: узнав о разрыве, SDK
 * ещё некоторое время шлёт `connected`, тогда как окно уже знает, что связи
 * нет. Свести их в одном месте дешевле, чем выяснять в каждом переходе,
 * откуда пришёл факт.
 *
 * Логики здесь нет: все решения — в `connectionMachine`. Этот файл только
 * доставляет факты и владеет временем жизни соединения.
 */
import { useEffect, useReducer, useRef } from "react";

import type { ConnectionMachineState } from "./connectionMachine";
import { initialConnectionState, transition } from "./connectionMachine";
import type { RealtimeClient, RealtimeClientOptions } from "./realtimeClient";
import { createRealtimeClient } from "./realtimeClient";

export interface UseRealtimeConnectionOptions {
  readonly centrifugoUrl: string;
  readonly channel: string;
  /**
   * Добытчик тикетов. Обязан быть **стабильным** между рендерами: новое
   * значение пересоздало бы соединение. Создаётся один раз вместе с
   * клиентом API (`createRealtimeTicketIssuer`), а не в теле компонента.
   */
  readonly issueTicket: RealtimeClientOptions["issueTicket"];
  /** Публикации канала — наружу, в срез 3. Не толкуются здесь. */
  readonly onPublication: (payload: unknown) => void;
  /** Подмена SDK — для тестов обвязки; в production не задаётся. */
  readonly createCentrifuge?: RealtimeClientOptions["createCentrifuge"];
}

/**
 * Состояние соединения для интерфейса.
 *
 * Возвращается всё состояние целиком, а не отдельные поля: `syncReason`
 * имеет смысл только вместе с `syncReason !== null`, а `reconnectAllowed`
 * объясняет, почему `online` не поднял соединение. Разложенные на три
 * независимых `useState` значения разошлись бы в момент перехода.
 */
export function useRealtimeConnection(
  options: UseRealtimeConnectionOptions,
): ConnectionMachineState {
  // Начальное состояние — из факта браузера, а не из предположения:
  // страница может быть открыта уже без сети, и тогда `CONNECTING` был бы
  // обещанием соединения, которого не будет.
  const [state, dispatch] = useReducer(transition, navigator.onLine, initialConnectionState);

  // Актуальное состояние для обработчика браузерных событий. Обработчик
  // регистрируется один раз и замкнуть переменную из рендера не может.
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  // Тот же приём для публикаций: подписка живёт дольше одного рендера, а
  // обработчик среза 3 меняется вместе с беседой.
  const publicationRef = useRef(options.onPublication);
  useEffect(() => {
    publicationRef.current = options.onPublication;
  }, [options.onPublication]);

  const clientRef = useRef<RealtimeClient | null>(null);

  const { centrifugoUrl, channel, issueTicket, createCentrifuge } = options;

  useEffect(() => {
    const client = createRealtimeClient({
      centrifugoUrl,
      channel,
      issueTicket,
      onEvent: dispatch,
      onPublication: (payload) => publicationRef.current(payload),
      createCentrifuge,
    });

    clientRef.current = client;
    client.start();

    return () => {
      clientRef.current = null;
      client.stop();
    };
  }, [centrifugoUrl, channel, issueTicket, createCentrifuge]);

  useEffect(() => {
    const goOffline = () => dispatch({ type: "browser-offline" });

    const goOnline = () => {
      // Читается состояние **до** этого перехода — тем самым `stateRef`
      // ещё держит признак, снятый терминальным разрывом. Так и нужно:
      // `reconnectAllowed` отвечает на вопрос «можно ли возобновлять», и
      // ответ на него не зависит от того, что сеть вернулась.
      const allowed = stateRef.current.reconnectAllowed;
      dispatch({ type: "browser-online" });
      if (allowed) {
        clientRef.current?.connect();
      }
    };

    window.addEventListener("offline", goOffline);
    window.addEventListener("online", goOnline);

    return () => {
      window.removeEventListener("offline", goOffline);
      window.removeEventListener("online", goOnline);
    };
  }, []);

  return state;
}
