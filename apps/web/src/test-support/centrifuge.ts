/**
 * Записывающий двойник SDK `centrifuge` — общий для тестов адаптера и обвязки.
 *
 * Лежит в `test-support/`, а не рядом с тестом, по двум причинам. Первая —
 * двойник нужен двоим: `realtimeClient.test.ts` проверяет разбор событий,
 * `useRealtimeConnection.test.ts` — что обвязка доносит их до автомата;
 * второй экземпляр разошёлся бы с первым молча. Вторая — этот модуль
 * **не** входит в production-граф (иначе он уехал бы в бандл), и проверка
 * изоляции, запрещающая импорт `centrifuge` вне адаптера, его не касается:
 * типы SDK здесь не упоминаются вовсе.
 *
 * Поверхность двойника — объявленная, а не «как получится»: `as unknown as
 * Centrifuge` живёт внутри и один раз. Двойник обязан быть **уже**
 * настоящего класса, иначе тест зеленел бы на поверхности, которой у SDK
 * нет.
 */
import type { CentrifugeFactory } from "../features/realtime/realtimeClient";

type Handler = (ctx?: unknown) => void;

export interface FakeCentrifuge {
  /** Передаётся в адаптер вместо конструктора SDK. */
  factory: CentrifugeFactory;
  /** Адрес, с которым адаптер обратился к библиотеке. */
  endpoint: string;
  /** Опции соединения, как их увидел бы SDK. */
  options: Record<string, unknown>;
  /** `getData` из опций — отдельно, чтобы вызывать его в тесте. */
  getData: (() => Promise<unknown>) | undefined;
  /** Имя канала подписки. */
  channel: string;
  clientHandlers: Record<string, Handler>;
  subscriptionHandlers: Record<string, Handler>;
  calls: { connect: number; disconnect: number; subscribe: number };
}

export function givenFakeCentrifuge(): FakeCentrifuge {
  // Один объект и есть двойник: `factory` пишет в него, тест читает из него.
  // Собирать двойник копией (`{ factory, ...state }`) нельзя — опции
  // появляются только в момент вызова фабрики, то есть после копирования, и
  // `getData` остался бы `undefined` навсегда. Тест на этом не падал бы
  // громко: он бы просто не нашёл `getData` — и красный пришёл бы не от того
  // дефекта, ради которого написан.
  const fake = {
    endpoint: "",
    options: {} as Record<string, unknown>,
    getData: undefined as (() => Promise<unknown>) | undefined,
    channel: "",
    clientHandlers: {} as Record<string, Handler>,
    subscriptionHandlers: {} as Record<string, Handler>,
    calls: { connect: 0, disconnect: 0, subscribe: 0 },
  } as unknown as FakeCentrifuge;

  fake.factory = (endpoint, options) => {
    fake.endpoint = endpoint;
    fake.options = options as Record<string, unknown>;
    fake.getData = options.getData as (() => Promise<unknown>) | undefined;

    return {
      on: (event: string, cb: Handler) => {
        fake.clientHandlers[event] = cb;
      },
      newSubscription: (channel: string) => {
        fake.channel = channel;
        return {
          on: (event: string, cb: Handler) => {
            fake.subscriptionHandlers[event] = cb;
          },
          subscribe: () => {
            fake.calls.subscribe += 1;
          },
        };
      },
      connect: () => {
        fake.calls.connect += 1;
      },
      disconnect: () => {
        fake.calls.disconnect += 1;
      },
    } as never;
  };

  return fake;
}

/** Тикет-счётчик: тест обязан различать «запрошен заново» и «подставлен тот же». */
export function givenTicketIssuer(): { issueTicket: () => Promise<string>; issued: () => number } {
  let issued = 0;
  return {
    issueTicket: async () => {
      issued += 1;
      return `ticket-${issued}`;
    },
    issued: () => issued,
  };
}
