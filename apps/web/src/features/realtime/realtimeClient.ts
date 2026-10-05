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
import { Centrifuge, UnauthorizedError as CentrifugeUnauthorizedError } from "centrifuge";
import type { Options } from "centrifuge";

import { SessionExpiredError, UnauthenticatedError } from "../../api/problems";
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
   *
   * Необязателен: без него соединение — **списка бесед** (телефон, экран списка, панели
   * беседы нет): личный канал и набор доходят, публикации ни одной беседы — нет, а факты
   * подписки на беседу автомату не передаются.
   */
  readonly channel?: string;
  /**
   * Личный канал — `user:{id}` (`packages/contracts/websocket/channels.json`).
   *
   * Сервер выдаёт его **тем же тикетом**, что и канал беседы
   * (`services/realtime.py:83`), то есть соединение остаётся одно, а каналов
   * у него два. Обязателен, а не необязателен: отсутствие личного канала
   * означало бы «публикации в него выбрасываются», и это ровно тот дефект,
   * который гейт и закрывает.
   */
  readonly userChannel: string;
  /**
   * Канал набора — `typing:{conversation_id}` (`channels.json`). Необязателен: без
   * него «печатает» выключено у этого клиента, а остальное работает как прежде.
   * Выдаёт его сервер тем же тикетом, что и канал беседы.
   */
  readonly typingChannel?: string;
  /**
   * Публикация **любого** канала набора `typing:{id}` — с именем канала, как
   * пришла. Каналов набора у соединения столько же, сколько бесед в тикете, и
   * именно поэтому уходит имя: в списке бесед «печатает» нужно и у тех бесед,
   * которые сейчас не открыты. Разбирает `typingState`, а не адаптер.
   */
  readonly onTypingPublication?: (channel: string, payload: unknown) => void;
  /** Свежий тикет на **каждую** попытку соединения (B5, B15). */
  readonly issueTicket: RealtimeTicketIssuer;
  /** Факты для автомата. Адаптер их не толкует. */
  readonly onEvent: (event: ConnectionEvent) => void;
  /** Публикация канала беседы — как пришла. Разбирает её срез 3, а не адаптер. */
  readonly onPublication?: (payload: unknown) => void;
  /** Публикация **личного** канала — `unread.changed`. Разбирается там же, где список. */
  readonly onUserPublication: (payload: unknown) => void;
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
  /**
   * Сообщить собеседникам «печатает» или «перестал». Fire-and-forget: событие
   * эфемерное, терять его нормально, и отказ (нет соединения, лимит сервера)
   * не показывается человеку и не повторяется.
   */
  sendTyping(signal: "typing" | "stop"): void;
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
    //
    // Потерянную сессию `issueTicket()` (через `fetchApi`, `api/client.ts`)
    // отклоняет `SessionExpiredError`/`UnauthenticatedError` — обычными
    // `Error`, о которых SDK ничего не знает. `_handleGetDataError`
    // (`build/index.js:4647`) узнаёт только собственный `UnauthorizedError`
    // и лишь тогда зовёт `_failUnauthorized()` (`reconnect = false`) —
    // любой другой отказ уходит в общую ветку ниже, где SDK планирует
    // следующую попытку по backoff и зовёт `getData` заново. Без этого
    // оборачивания истёкшая сессия не останавливала SDK вовсе: `getData`
    // отказывал на каждой попытке переподключения бесконечно, а каждая
    // попытка — это свежий `POST /realtime/token` (и, если он `401`, ещё
    // `POST /auth/refresh` изнутри `fetchApi`) на сервер, для открытой
    // вкладки с истёкшей сессией — навсегда, а не до следующей перезагрузки.
    getData: async () => {
      try {
        return { ticket: await options.issueTicket() };
      } catch (error) {
        if (error instanceof SessionExpiredError || error instanceof UnauthenticatedError) {
          throw new CentrifugeUnauthorizedError(error.message);
        }
        throw error;
      }
    },

    // Холодное рукопожатие не укладывается в дефолтный таймаут SDK, и это
    // измерено на стенде, а не выведено из общего соображения.
    //
    // Механизм обрыва — в SDK: `connectTimeout = setTimeout(() =>
    // transport.close(), this._config.timeout)` (`build/index.js:4444-4447`),
    // снимается только в `onOpen`, а дефолт — `timeout: 5000` (`:3635`).
    //
    // Десять секунд холодного рукопожатия — не сеть, не кластер и не TLS, а
    // резолвер macOS. Домен `local` у него обслуживает отдельный резолвер
    // (`scutil --dns`: `resolver #2: domain : local, options : mdns,
    // timeout : 5`), и запрос **AAAA** упирается в его таймаут, потому что в
    // `/etc/hosts` для `*.finops.local` есть только IPv4-строки. Замер на
    // одном и том же имени: A — 9 мс, AAAA — 5015 мс, полный `getaddrinfo`
    // (спрашивает оба семейства) — 5010 мс. Кэша у резолвера нет: три
    // обращения подряд дали 5010, 10015 и 15018 мс.
    //
    // Отсюда и «почему десять, а не пять»: Chromium платит **два** таких
    // резолва на одно холодное соединение — 9382, 10019, 10065, 10145 и
    // 9420 мс в пяти измерениях, тогда как `curl` платит один (5.038 с,
    // `time_namelookup=5.004`). Схема ни при чём: `http` и `https` стоят
    // одинаково. Второе обращение в том же контексте браузера — 4–25 мс.
    //
    // Почему это не «медленно, но со второй попытки»: оборванная попытка кэш
    // резолвера не греет. С дефолтным таймаутом восемь попыток подряд за
    // 45 секунд умерли на 5001–5005 мс (`error` → `close 1006`), а тот же
    // холодный контекст с `timeout: 30_000` открыл сокет на ~10 с и получил
    // от сервера настоящий ответ по протоколу (`disconnected:4501` — отказ в
    // авторизации для пробы без тикета). Таймаут ниже холодной стоимости
    // означает, что соединения не будет **никогда**, а не «придёт следующим».
    //
    // Тридцать секунд — втрое больше худшего измерения: разброс уже виден
    // (9124 и 9382), и упираться в измерение нельзя. Тот же `timeout` SDK
    // применяет к ответам на подписку и команды (`:955`, `:5473`), но там он
    // ограничивает ожидание ответа, а не рукопожатие.
    timeout: 30_000,
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
    // Фильтр остаётся, и он несущий, а не забытый: `subscribed` приходит на
    // **каждый** выданный канал (`services/realtime.py:83` выдаёт оба одним
    // списком), и пущенный в автомат личный канал читался бы как подписка этой
    // беседы — то есть `data-connection-state` уходил бы из `connected` по
    // событию о другом канале. Проверяется мутацией: снятие фильтра краснит
    // автомат, а не косметику.
    if (options.channel === undefined || ctx.channel !== options.channel) return;

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
    // Маршрутизация — по **имени** канала, а не по «наш/чужой». Прежде здесь
    // стоял именно «чужой», и `unread.changed` в `user:{id}` не доходил до
    // клиента вовсе: вкладка узнавала о своём числе только перезагрузкой.
    // Третий канал (которого сервер не выдаёт) не уходит никуда: неизвестное
    // имя — не повод отдать публикацию первому попавшемуся обработчику.
    if (options.channel !== undefined && ctx.channel === options.channel) {
      options.onPublication?.(ctx.data);
      return;
    }
    if (ctx.channel === options.userChannel) {
      options.onUserPublication(ctx.data);
      return;
    }
    // Только пространство `typing:`: неизвестное имя по-прежнему не уходит
    // никуда — третий канал не повод отдать публикацию первому обработчику.
    if (ctx.channel.startsWith("typing:")) options.onTypingPublication?.(ctx.channel, ctx.data);
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
    sendTyping(signal) {
      if (options.typingChannel === undefined) return;
      // Тело — минимум: автора и срок задаёт сервер (publish-proxy, `RT-003`),
      // и всё, что клиент добавил бы сверх `state`, он отбросит.
      client.publish(options.typingChannel, { state: signal }).catch(() => {
        // Нет соединения или лимит: набор терять нормально.
      });
    },
  };
}


/** Соединение звонков: свой канал `call:{id}`, свой билет, своя жизнь. */
export interface CallChannelClientOptions {
  readonly centrifugoUrl: string
  /** `call:{user_id}` (`channels.json`). */
  readonly channel: string
  /** Билет **только на канал звонков**: `POST /realtime/token?scope=calls`. */
  readonly issueTicket: RealtimeTicketIssuer
  readonly onPublication: (payload: unknown) => void
  /** Соединение поднято или потеряно — для подсказки в интерфейсе звонка. */
  readonly onConnectionChange?: (connected: boolean) => void
  readonly createCentrifuge?: CentrifugeFactory
}

export interface CallChannelClient {
  start(): void
  stop(): void
}

/**
 * Отдельное соединение для звонков, а не ещё один канал соединения беседы.
 *
 * Соединение беседы пересоздаётся при смене открытой беседы
 * (`useRealtimeConnection`: канал входит в зависимости эффекта), а канал звонков
 * **истории не имеет** (`channels.json`): сигнал, пришедший в окно между
 * закрытием старого соединения и открытием нового, пропал бы навсегда, и
 * звонок не установился бы. Поэтому у звонков своё соединение, которое живёт,
 * пока открыто приложение, и не знает, какая беседа сейчас на экране.
 *
 * Билет другой — только на канал звонков: соединение не получает сообщений
 * бесед и набора, ему они не нужны.
 */
export function createCallChannelClient(options: CallChannelClientOptions): CallChannelClient {
  const create = options.createCentrifuge ?? ((endpoint, config) => new Centrifuge(endpoint, config))

  const client = create(options.centrifugoUrl, {
    // Тот же приём, что у соединения беседы: билет берётся на **каждую** попытку,
    // а потерянная сессия останавливает переподключение, а не крутит его вечно.
    getData: async () => {
      try {
        return { ticket: await options.issueTicket() }
      } catch (error) {
        if (error instanceof SessionExpiredError || error instanceof UnauthenticatedError) {
          throw new CentrifugeUnauthorizedError(error.message)
        }
        throw error
      }
    },
    // Холодное рукопожатие на macOS длится ~10 с (резолвер `.local`): см. таймаут
    // соединения беседы выше — по той же причине и тем же значением.
    timeout: 30_000,
  })

  client.on("connected", () => options.onConnectionChange?.(true))
  client.on("disconnected", () => options.onConnectionChange?.(false))
  client.on("connecting", () => options.onConnectionChange?.(false))
  client.on("publication", (ctx) => {
    if (ctx.channel === options.channel) options.onPublication(ctx.data)
  })

  return {
    start: () => client.connect(),
    stop: () => client.disconnect(),
  }
}
