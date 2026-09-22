/**
 * Адаптер к `centrifuge` — **единственный** модуль, который его импортирует.
 *
 * Здесь заканчивается SDK и начинаются наши факты: наружу выходят только
 * `ConnectionEvent`, которые понимает автомат. Причина разделения не
 * стилистическая — `connectionMachine` обязан тестироваться без сети, а
 * модуль, знающий SDK, без SDK не соберётся. Проверяется статически:
 * последний блок `realtimeClient.test.ts` краснеет, если `centrifuge`
 * появится ещё в одном production-модуле `src/` — и называет этот модуль.
 *
 * Все имена и поля ниже — измеренные у закреплённого артефакта `centrifuge`
 * `5.7.4`, а не взятые из документации более новой версии (таблица в
 * `README.md` этого каталога).
 */
import { Centrifuge } from "centrifuge";
import type { Options } from "centrifuge";

import type { RealtimeTicketIssuer } from "../../api/realtimeToken";
import type { ConnectionEvent } from "./connectionMachine";

/**
 * Ошибка недостижимой позиции (`112`) в этом режиме **не наблюдаема** — и это
 * измерено, а не предположено.
 *
 * Код `112` доходит до приложения только путём **клиентской** подписки:
 * `_handleUnsubscribe` (`centrifuge/build/index.js:5317`) берёт подписку
 * `_getSub(channel, 0)` и, лишь когда она **есть**, зовёт
 * `sub._setUnsubscribed(unsubscribe.code, …)` (`:5330`) — с кодом эмитит
 * событие объект подписки. У server-side подписки такого объекта нет вовсе:
 * её канал живёт в `_serverSubs`, а `_getSub` (`:5065`) смотрит только
 * клиентские `_subs`. Поэтому SDK уходит в ветку `:5321` и эмитит
 * `client.on("unsubscribed", { channel })` — **без кода**, хотя
 * `unsubscribe.code` в области видимости. Иного входа тоже нет: `subscribing`
 * (`:4263`, `:5306`), `error` и `disconnected` кода подписки не несут.
 *
 * Отсюда — то, что план B6 и предписывал на такой замер: `"unrecoverable-position"`
 * из набора `syncReason` уходит, и события автомата с этим именем нет.
 * Путь, записанный под событие, которого на проводе не бывает, — тот же класс
 * дефекта, что обработчик на `disconnected(112)`.
 */

/** Что нужно адаптеру, чтобы поднять соединение. Всё — снаружи, ничего из окружения. */
export interface RealtimeClientOptions {
  /** Публичный адрес: `wss://rt.finops.local/connection/websocket` (`runtime-config.ts`). */
  readonly centrifugoUrl: string;
  /**
   * Канал беседы — `conversation:{conversation_id}` (`packages/contracts/websocket/channels.json`).
   *
   * Роль у него здесь **отборная, а не выбирающая**: подписку выдаёт сервер, и
   * тем же событием приходят каналы, к этой беседе не относящиеся (`user:{id}`).
   * Имя говорит, какой из них наш.
   */
  readonly channel: string;
  /** Свежий тикет на **каждую** попытку соединения (B5, B15). */
  readonly issueTicket: RealtimeTicketIssuer;
  /** Факты для автомата. Адаптер их не толкует. */
  readonly onEvent: (event: ConnectionEvent) => void;
  /** Публикация канала — как пришла. Разбирает её срез 3, а не адаптер. */
  readonly onPublication: (payload: unknown) => void;
  /**
   * Подмена конструктора SDK — для тестов адаптера.
   *
   * Параметром, а не через `vi.mock`: подмена модуля целиком подменила бы и
   * самого адаптера, то есть тест проверял бы не тот код, который исполняется.
   * Здесь подменяется ровно шов с SDK.
   */
  readonly createCentrifuge?: CentrifugeFactory;
}

export type CentrifugeFactory = (endpoint: string, options: Partial<Options>) => Centrifuge;

/** Что возвращает адаптер композиции: поднять, возобновить и опустить соединение. */
export interface RealtimeClient {
  start(): void;
  /**
   * Возобновить попытки после того, как браузер вернулся в сеть.
   *
   * Отдельным методом, а не повторным `start()`: поднимать больше нечего —
   * `connect()` и есть весь подъём. И вызывается он **нами**, а не ожиданием
   * SDK: измерено (`build/index.js:4186`), что свой обработчик `online` SDK
   * применяет только к состоянию `Connecting`, тогда как после потери сети он
   * уходит в `Disconnected`. Полагаться на его таймер значило бы, что момент
   * возобновления выбирает не наш признак `reconnectAllowed`, а внутренний
   * backoff библиотеки.
   */
  connect(): void;
  stop(): void;
}

export function createRealtimeClient(options: RealtimeClientOptions): RealtimeClient {
  const create = options.createCentrifuge ?? ((endpoint, config) => new Centrifuge(endpoint, config));

  const client = create(options.centrifugoUrl, {
    // `getData`, а не `data`, и это несущая деталь, а не предпочтение.
    //
    // Измерено (`build/index.js:4584`): `_connect()` вызывает
    // `this._config.getData()` **на каждой попытке**, включая первую, и
    // кладёт результат в `_data` перед открытием транспорта. Тикет живёт
    // 120 секунд (`token_ttl_seconds` в `adapters/centrifugo.py`), поэтому
    // значение, взятое один раз при загрузке страницы, просрочено ровно в
    // том сценарии, ради которого этот гейт существует, — после окна
    // офлайна. `data` задало бы начальное значение и на переподключении
    // осталось бы прежним.
    //
    // `setData` не используется: он выражает то же самое слабее и, по
    // измеренному комментарию SDK, **перекрывается** `getData`.
    getData: async () => ({ ticket: await options.issueTicket() }),
  });

  // Все события — **клиентские**, и подписки, созданной клиентом, здесь нет.
  //
  // Так устроена server-side подписка: каналы выдаёт сервер в connect-ответе
  // (`channels` в `services/realtime.py:83`, `api/main.py:593`; «Клиент не
  // выбирает канал сам», `channels.json:5`), SDK принимает их в
  // `_processServerSubs` (`centrifuge/build/index.js:5149`) и эмитит
  // `client.on("subscribed")` и `client.on("publication")` с полем `ctx.channel`.
  // `newSubscription()` + `subscribe()` — это **другой** режим, клиентский, и в
  // нём события приходят объекту подписки. Оставить его значило бы моделировать
  // на клиенте то, чего сервер не делает: канал выбран не нами.
  //
  // Двойник в тестах исправлен тем же коммитом: он давал клиентскую подписку,
  // то есть проверял не тот режим, который работает на стенде.

  client.on("connecting", () => options.onEvent({ type: "sdk-connecting" }));
  client.on("connected", () => options.onEvent({ type: "sdk-connected" }));
  client.on("disconnected", (ctx) =>
    options.onEvent({ type: "sdk-disconnected", code: ctx.code }),
  );

  client.on("subscribed", (ctx) => {
    // Чужой канал — не наш факт: `user:{id}` приходит тем же событием
    // (`services/realtime.py:83`), и докладывать о нём автомату беседы значило
    // бы принять чужую подписку за подписку этой беседы.
    if (ctx.channel !== options.channel) return;

    options.onEvent({
      type: "subscription-subscribed",
      // Обе половины обязательны: `recovered` равно `false` и тогда, когда
      // восстанавливать было нечего, — на первой подписке. Различает эти
      // случаи только `wasRecovering`.
      wasRecovering: ctx.wasRecovering,
      recovered: ctx.recovered,
    });
  });

  client.on("publication", (ctx) => {
    if (ctx.channel !== options.channel) return;
    options.onPublication(ctx.data);
  });

  return {
    start() {
      // Только `connect()`: подписки, которую надо было бы заводить отдельно,
      // в этом режиме нет — сервер подписывает клиента сам, по тикету.
      client.connect();
    },
    connect() {
      // Измерено (`build/index.js:3897`): повторный вызов на уже
      // подключённом или подключающемся клиенте ничего не делает. Поэтому
      // звать его из браузерного `online` безопасно, даже если SDK успел
      // начать попытку сам.
      client.connect();
    },
    stop() {
      client.disconnect();
    },
  };
}
