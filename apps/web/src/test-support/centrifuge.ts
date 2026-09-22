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
 *
 * **Двойник моделирует server-side подписку, а не клиентскую**, и это его
 * главное свойство, а не деталь. Канал клиенту не принадлежит: сервер выдаёт
 * его в connect-ответе (`channels` в `services/realtime.py:83`,
 * `api/main.py:593`), «Клиент не выбирает канал сам» (`channels.json:5`), а SDK
 * принимает каналы и эмитит события **клиента** — `client.on("subscribed" |
 * "publication")` — с полем `ctx.channel` (`_processServerSubs`,
 * `centrifuge/build/index.js:5149`; `ClientEvents`, `build/types.d.ts:20-45`).
 *
 * Поэтому `subscriptionHandlers` здесь **нет** и `newSubscription` только
 * считается. Двойник, умеющий и клиентскую подписку тоже, позволял бы тесту
 * изобразить режим, которого на стенде не бывает, — и зелёный прогон говорил
 * бы о коде, который не исполняется. Ровно это и случилось до правки: адаптер
 * заводил `newSubscription()`, двойник её обслуживал, а сервер подписывает
 * клиента сам.
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
  /** Обработчики клиентских событий — единственный вход фактов в этом режиме. */
  clientHandlers: Record<string, Handler>;
  /**
   * `newSubscription` **считается**, а не обслуживается: в server-side режиме
   * её вызов — уже дефект, и тест обязан называть его числом, а не падением
   * внутри двойника.
   */
  calls: { connect: number; disconnect: number; newSubscription: number };
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
    clientHandlers: {} as Record<string, Handler>,
    calls: { connect: 0, disconnect: 0, newSubscription: 0 },
  } as unknown as FakeCentrifuge;

  fake.factory = (endpoint, options) => {
    fake.endpoint = endpoint;
    fake.options = options as Record<string, unknown>;
    fake.getData = options.getData as (() => Promise<unknown>) | undefined;

    return {
      on: (event: string, cb: Handler) => {
        fake.clientHandlers[event] = cb;
      },
      // Заглушка, а не рабочая подписка: её задача — довести вызов до счётчика
      // и не уронить тест `TypeError`-ом раньше, чем он назовёт дефект
      // ассертом. Обработчики никуда не пишутся: у серверной подписки их и
      // неоткуда взять, поэтому тест, навесивший их здесь, обязан покраснеть.
      newSubscription: () => {
        fake.calls.newSubscription += 1;
        return { on: () => {}, subscribe: () => {}, unsubscribe: () => {} };
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
