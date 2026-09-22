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
 * «Unrecoverable Position Error» — ошибка класса **подписки**, а не разрыв.
 *
 * Измерено (`build/index.js:1595`, `_subscribeError`): код `>= 100` уходит
 * постоянным путём, `!== 109`, `temporary` не выставлен — значит
 * `_setUnsubscribed(err.code, err.message, false)`, то есть подписка
 * эмитит **`unsubscribed`**, а не `disconnected`. Обработчик на
 * `disconnected(112)` был бы обработчиком на событии, которое не наступает.
 */
export const UNRECOVERABLE_POSITION = 112;

/** Что нужно адаптеру, чтобы поднять соединение. Всё — снаружи, ничего из окружения. */
export interface RealtimeClientOptions {
  /** Публичный адрес: `wss://rt.finops.local/connection/websocket` (`runtime-config.ts`). */
  readonly centrifugoUrl: string;
  /**
   * Канал беседы — `conversation:{conversation_id}` (`packages/contracts/websocket/channels.json`).
   * Имя приходит снаружи, потому что канал выбирает композиция, а не адаптер.
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
   * константу `UNRECOVERABLE_POSITION`, то есть тест проверял бы не тот код,
   * который исполняется. Здесь подменяется ровно шов с SDK.
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
   * Отдельным методом, а не повторным `start()`, потому что подписка при
   * этом не пересоздаётся: `subscribe()` вызывается один раз, а
   * переподключением занимается `connect()`. И вызывается он **нами**, а не
   * ожиданием SDK: измерено (`build/index.js:4186`), что свой обработчик
   * `online` SDK применяет только к состоянию `Connecting`, тогда как после
   * потери сети он уходит в `Disconnected`. Полагаться на его таймер значило
   * бы, что момент возобновления выбирает не наш признак `reconnectAllowed`,
   * а внутренний backoff библиотеки.
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

  const subscription = client.newSubscription(options.channel);

  // Клиентские события — факты о канале до подписки.
  client.on("connecting", () => options.onEvent({ type: "sdk-connecting" }));
  client.on("connected", () => options.onEvent({ type: "sdk-connected" }));
  client.on("disconnected", (ctx) =>
    options.onEvent({ type: "sdk-disconnected", code: ctx.code }),
  );

  // События подписки — факты о данных в канале.
  subscription.on("subscribed", (ctx) =>
    options.onEvent({
      type: "subscription-subscribed",
      // Обе половины обязательны: `recovered` равно `false` и тогда, когда
      // восстанавливать было нечего, — на первой подписке. Различает эти
      // случаи только `wasRecovering`.
      wasRecovering: ctx.wasRecovering,
      recovered: ctx.recovered,
    }),
  );

  subscription.on("unsubscribed", (ctx) => {
    if (ctx.code === UNRECOVERABLE_POSITION) {
      options.onEvent({ type: "unrecoverable-position" });
    }
  });

  subscription.on("publication", (ctx) => options.onPublication(ctx.data));

  return {
    start() {
      // Подписка раньше соединения: `subscribe()` только регистрирует
      // намерение, а команда уезжает, когда транспорт откроется.
      subscription.subscribe();
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
