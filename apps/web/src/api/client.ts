/**
 * Обёртка над сгенерированным клиентом — единственное место, где живут адрес
 * API, `Authorization`, `X-Device-Id`, `credentials` и разбор отказа.
 *
 * Разложенное по вызывающим, это расходится молча: заголовок устройства,
 * забытый в одном вызове из девяти, не виден ни `tsc`, ни линтеру, а сервер
 * в ответ на вызов без заголовка не ошибается, а заводит новое устройство
 * (`_device_for`, `services/identity.py`) — то есть строка в `devices`
 * на каждый такой вызов.
 */
import { Configuration, type ConversationListPage, type FetchAPI, type Me } from "./generated";
import {
  ApiProblem,
  ServiceUnavailableError,
  SessionExpiredError,
  UnauthenticatedError,
  problemFromResponse,
  traceIdFromResponse,
} from "./problems";

/**
 * Базовый путь **относительный**.
 *
 * `servers` контракта документирует публичный адрес
 * (`https://app.finops.local/api/v1`), и это его частное дело: браузер обязан
 * звать собственный origin. Абсолютный адрес увёл бы запрос с того origin, на
 * котором стоит refresh-cookie с `SameSite=Strict`, — и вход перестал бы
 * восстанавливаться вовсе.
 */
export const API_BASE_PATH = "/api/v1";

/**
 * Учётные данные запрашиваются **явно**.
 *
 * Браузер и без настройки послал бы same-origin cookie, но «и без настройки
 * послал бы» — это неявность, на которой не стоит держать refresh-токен.
 * `"include"` запрещён: он отправил бы cookie чужому origin, а это ровно та
 * граница, ради которой на cookie стоит `SameSite=Strict`.
 */
export const CREDENTIALS: RequestCredentials = "same-origin";

/** Заголовок устройства. Сервер читает его на **каждом** защищённом пути. */
export const DEVICE_ID_HEADER = "X-Device-Id";

/** Обмен кода авторизации — единственный вызов, где устройство есть и в теле. */
export const AUTH_CALLBACK_PATH = "/auth/callback";

/** Обмен refresh-cookie. Тела у этого вызова нет и в контракте. */
export const AUTH_REFRESH_PATH = "/auth/refresh";

/**
 * Исход обмена токена — он же исход первичного входа, потому что механика одна.
 *
 * `401` и `503` здесь **разные** исходы: сервер разделяет их намеренно
 * (`_login_failure`, `api/main.py`) — «`503` означает „повтори позже“, `401` —
 * „войди заново“. Слить их в один ответ значило бы отправлять человека на
 * повторный вход в момент, когда вход всё равно не работает».
 */
export type LoginOutcome =
  | { readonly kind: "ok"; readonly deviceId: string | undefined }
  | { readonly kind: "unauthenticated" }
  | { readonly kind: "unavailable"; readonly traceId: string | undefined };

/**
 * Состояние загрузки. Три отказа различимы, потому что требуют разного от
 * пользователя: войти, подождать и повторить, или войти заново.
 */
export type BootState =
  | { readonly kind: "bootstrapping" }
  /**
   * Сессии не было вовсе — у первого посетителя cookie просто нет.
   * Слова «истекла» здесь быть не должно: это утверждение о том, чего не было.
   */
  | { readonly kind: "unauthenticated" }
  /** Сессия **была** и кончилась: `401` на обмене после `ready`. */
  | { readonly kind: "session-expired" }
  /** Сервис недоступен — повторяемое состояние, повод для кнопки «повторить». */
  | { readonly kind: "transient-error"; readonly traceId: string | undefined }
  /**
   * Готовность — утверждение о **данных**, а не о токене.
   *
   * Поэтому в состоянии лежат сами ответы `/me` и `/conversations`, а не
   * признак «мы их получили»: композиции нужны и оба, и место, где они
   * разбираются один раз. Иначе она либо ходила бы за ними второй раз, либо
   * держала второй признак готовности, способный разойтись с этим.
   *
   * Тип именно транспортный (`Me`, `ConversationListPage`), а не модель
   * интерфейса: адаптация к модели — дело `features/**`, и она обязана видеть
   * то, что сервер действительно сказал, включая поля, которых интерфейс не
   * показывает.
   */
  | { readonly kind: "ready"; readonly account: Me; readonly conversations: ConversationListPage };

/** Тело `AccessToken` по проводу. Имена — как в контракте, до конвертера. */
interface AccessTokenBody {
  readonly access_token?: string;
  readonly device_id?: string;
}

/**
 * Данные, без которых `ready` не наступает.
 *
 * Возвращаемые типы — транспортные, а не `unknown`: без этого `ready` пришлось
 * бы наполнять приведением, и обещание «готов только с данными» держалось бы
 * на честном слове.
 */
export interface BootstrapDependencies {
  readonly loadAccount: () => Promise<Me>;
  readonly loadConversations: () => Promise<ConversationListPage>;
}

export interface ApiClientOptions {
  /** Подмена `fetch` в тестах. */
  readonly fetchImpl?: FetchAPI;
  /**
   * Текущий идентификатор устройства.
   *
   * Функция, а не значение: идентификатор заводится **до** первого запроса и
   * меняется, когда сервер назвал свой, — а первый запрос (refresh) уже обязан
   * нести заголовок. Взять его из ответа нельзя, иначе петля.
   */
  readonly deviceId?: () => string | null;
  /**
   * Принять серверный идентификатор.
   *
   * Вызывается, когда ответ принёс **другой** идентификатор: занятый
   * `_device_for` не отвергает ошибкой, а заменяет новым. Клиент, оставшийся
   * при своём, слал бы чужой идентификатор на каждом следующем вызове —
   * и получал бы новое устройство не один раз, а каждый раз.
   */
  readonly saveDeviceId?: (deviceId: string) => void;
}

export interface ApiClient {
  /** Конфигурация для сгенерированных API; база в ней — относительная. */
  readonly configuration: Configuration;
  /** `fetch`, который видят сгенерированные API: заголовки и развилка тут. */
  readonly fetchApi: FetchAPI;
  readonly basePath: string;
  readonly hasSession: () => boolean;
  readonly setAccessToken: (accessToken: string) => void;
  /** Сессия подтверждена данными bootstrap — отличает `sessionExpired` от `unauthenticated`. */
  readonly markSessionEstablished: () => void;
  readonly exchangeAuthorizationCode: (params: {
    readonly code: string;
    readonly codeVerifier: string;
    readonly redirectUri: string;
    /**
     * Идёт и заголовком, и телом: тело читается только этим вызовом.
     *
     * Необязателен по контракту (`AuthorizationCode`, поле `device_id` —
     * optional) и намеренно необязателен здесь. Отсутствие — не ошибка: его
     * причина называется явно. `code_verifier` пережил хранилище, а
     * идентификатор — нет (человек очистил `localStorage` между уходом на
     * Keycloak и возвратом); сервер такую потерю переживает — чеканит новое
     * устройство и возвращает его, — и клиент принимает его как canonical.
     * Запрещать обмен из-за потерянной метки значило бы отказывать человеку в
     * входе за то, что он почистил браузер.
     */
    readonly deviceId?: string;
  }) => Promise<LoginOutcome>;
  readonly refreshAccessToken: () => Promise<LoginOutcome>;
  readonly bootstrap: (deps: BootstrapDependencies) => Promise<BootState>;
}

function isAbortError(cause: unknown): boolean {
  return (
    typeof cause === "object" &&
    cause !== null &&
    "name" in cause &&
    (cause as { readonly name?: unknown }).name === "AbortError"
  );
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const fetchImpl: FetchAPI = options.fetchImpl ?? ((input, init) => fetch(input, init));
  const currentDeviceId = options.deviceId ?? (() => null);

  /**
   * Access token живёт **только здесь** — в замыкании, то есть в памяти
   * вкладки. Не в `localStorage`, не в `sessionStorage`: переживший перезагрузку
   * токен — это удостоверение в хранилище, а `ADR 0005` говорит «`localStorage`
   * для удостоверений не используется никогда». Состояние восстанавливается
   * refresh-cookie, а не сохранённым токеном.
   */
  let accessToken: string | null = null;

  /** Сессия была подтверждена данными bootstrap, а не только обменом. */
  let sessionEstablished = false;

  /**
   * Один разделяемый `Promise` на вкладку.
   *
   * Два одновременных `401` не должны запускать два обмена одной и той же
   * вращающейся refresh-cookie: второй получил бы уже использованный токен.
   * Контракт прямо говорит, что одновременные запросы — норма
   * (`/auth/refresh`: «Несколько вкладок обменяют каждая свой, и одновременные
   * запросы — норма»), поэтому сведение к одному обмену — политика клиента,
   * а не обещание сервера.
   */
  let refreshInFlight: Promise<LoginOutcome> | null = null;

  /** Потеря сессии: чем она была — вопрос контекста, а не кода ответа. */
  function sessionLost(): Error {
    return sessionEstablished ? new SessionExpiredError() : new UnauthenticatedError();
  }

  /** Единственное место, где собираются заголовки и `credentials`. */
  async function send(
    input: RequestInfo | URL,
    init: RequestInit | undefined,
    token: string | null,
  ): Promise<Response> {
    const headers = new Headers(init?.headers);
    if (token !== null) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    const deviceId = currentDeviceId();
    if (deviceId !== null) {
      headers.set(DEVICE_ID_HEADER, deviceId);
    }
    try {
      return await fetchImpl(input, { ...init, headers, credentials: CREDENTIALS });
    } catch (cause) {
      // Отмена — не недоступность сервиса: размонтированный компонент снял
      // запрос, и показывать «сервис недоступен» за это нельзя.
      if (isAbortError(cause)) {
        throw cause;
      }
      throw new ServiceUnavailableError("Сервис недоступен");
    }
  }

  /** Не-2xx, кроме `401`: его развилку разбирает `fetchApi`. */
  async function check(response: Response): Promise<Response> {
    if (response.status === 401) {
      throw sessionLost();
    }
    if (response.status === 503) {
      throw new ServiceUnavailableError("Сервис недоступен", traceIdFromResponse(response));
    }
    if (!response.ok) {
      throw await problemFromResponse(response);
    }
    return response;
  }

  function acceptDeviceId(deviceId: string | undefined): void {
    if (deviceId === undefined || deviceId === currentDeviceId()) {
      return;
    }
    options.saveDeviceId?.(deviceId);
  }

  /**
   * Обмен токена — общая механика callback и refresh.
   *
   * Отличаются они ровно телом: у callback оно есть, у refresh его нет и в
   * контракте. Заголовок устройства при этом несут оба: refresh без
   * `X-Device-Id` не переиспользует устройство, а чеканит новое.
   */
  async function login(path: string, body: unknown | null): Promise<LoginOutcome> {
    const init: RequestInit = { method: "POST" };
    if (body !== null) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }

    // Сетевой отказ — третий исход обмена, и он не ответ: `Response` не
    // приходит вовсе, поэтому ни `status`, ни `trace_id` взять неоткуда. Без
    // этой ветки отклонение уходило бы наружу мимо разбора, и вызывающий
    // оставался бы без состояния вовсе — `bootstrap` не выставил бы даже
    // `transient-error`, а человек смотрел бы на «Loading…» бесконечно.
    let response: Response;
    try {
      response = await send(`${API_BASE_PATH}${path}`, init, null);
    } catch (cause) {
      // Отмена — не отказ обмена: снятый запрос не говорит о сервисе ничего,
      // и трогать по нему токен нельзя. Уходит наверх как есть.
      if (isAbortError(cause)) {
        throw cause;
      }
      if (cause instanceof ServiceUnavailableError) {
        // Токен обесценивается тем же правилом, что и в развилке ниже, и это
        // не формальность: сетевой отказ не даёт узнать, дошёл ли запрос до
        // сервера, а тот снимает refresh-cookie при любом неудачном обмене.
        accessToken = null;
        return { kind: "unavailable", traceId: cause.traceId };
      }
      // Всё прочее здесь — неожиданность, а не состояние: пусть падает громко.
      throw cause;
    }

    if (response.ok) {
      const accessTokenBody = (await response.json()) as AccessTokenBody;
      const token = accessTokenBody.access_token;
      if (typeof token !== "string" || token === "") {
        // `200` без токена — не состояние интерфейса, а расхождение с
        // контрактом: показать «войдите» здесь значило бы скрыть дефект.
        throw new Error("Ответ обмена токена не содержит access_token");
      }
      accessToken = token;
      acceptDeviceId(accessTokenBody.device_id);
      return { kind: "ok", deviceId: accessTokenBody.device_id };
    }
    // Любой отказ обмена обесценивает прежний токен, и правило стоит **до**
    // развилки, а не в одной её ветке. Сервер отверг T ответом `401` ещё до
    // того, как этот обмен был запущен, а refresh-cookie он снимает при любом
    // неудачном обмене, включая `503` (`api/main.py:377`). Оставить T в памяти
    // значило бы получить состояние, которого не бывает: `hasSession()` истинно
    // рядом с `transient-error`, и следующий запрос уходит с токеном, о котором
    // заведомо известно, что он не работает.
    //
    // `sessionEstablished` при этом **не** сбрасывается: «валидного токена нет»
    // и «сессии не было» — разные факты, и следующий `401` на обмене обязан
    // остаться «истекла», а не превратиться в «первый визит».
    accessToken = null;

    if (response.status === 401) {
      return { kind: "unauthenticated" };
    }
    if (response.status >= 500) {
      return { kind: "unavailable", traceId: traceIdFromResponse(response) };
    }
    throw await problemFromResponse(response);
  }

  /**
   * `fetch`, который видят сгенерированные API.
   *
   * `401` — одна попытка обмена, затем повтор исходного запроса. Одна, а не
   * цикл: повторный `401` означает, что сессии больше нет, и зацикливание
   * на обмене сожгло бы её остатки.
   */
  const fetchApi: FetchAPI = async (input, init) => {
    const first = await send(input, init, accessToken);
    if (first.status !== 401) {
      return check(first);
    }

    const outcome = await refreshAccessToken();
    if (outcome.kind === "ok") {
      return check(await send(input, init, accessToken));
    }
    if (outcome.kind === "unavailable") {
      // `503` на обмене — транзитное состояние, а не потеря сессии: сервер
      // отвечает так, когда недоступен источник ключей, и «войди заново»
      // отправило бы человека входить в момент, когда вход не работает.
      throw new ServiceUnavailableError("Обмен токена отложен: сервис недоступен", outcome.traceId);
    }
    throw sessionLost();
  };

  function refreshAccessToken(): Promise<LoginOutcome> {
    if (refreshInFlight === null) {
      refreshInFlight = login(AUTH_REFRESH_PATH, null).finally(() => {
        refreshInFlight = null;
      });
    }
    return refreshInFlight;
  }

  async function bootstrap(deps: BootstrapDependencies): Promise<BootState> {
    const outcome = await refreshAccessToken();
    if (outcome.kind === "unavailable") {
      return { kind: "transient-error", traceId: outcome.traceId };
    }
    if (outcome.kind === "unauthenticated") {
      // Cookie у первого посетителя просто нет — это не истечение сессии.
      return { kind: "unauthenticated" };
    }

    let account: Me;
    let conversations: ConversationListPage;
    try {
      [account, conversations] = await Promise.all([deps.loadAccount(), deps.loadConversations()]);
    } catch (error) {
      return bootstrapFailure(error);
    }

    // `ready` — только здесь, после данных, и вместе с ними. «Токен есть» и
    // «клиент готов» — разные утверждения: между ними стоят `/me` и
    // `/conversations`, и любой из них может ответить `503`. Запросы идут
    // параллельно, но адаптация к модели — после получения **обоих**: без
    // `user_id` из `/me` «собеседник» в списке неотличим от самого зрителя.
    sessionEstablished = true;
    return { kind: "ready", account, conversations };
  }

  function bootstrapFailure(error: unknown): BootState {
    if (error instanceof ServiceUnavailableError) {
      return { kind: "transient-error", traceId: error.traceId };
    }
    if (error instanceof SessionExpiredError) {
      return { kind: "session-expired" };
    }
    if (error instanceof UnauthenticatedError) {
      return { kind: "unauthenticated" };
    }
    if (error instanceof ApiProblem && error.status >= 500) {
      // Пятьсот на `/me` во время загрузки — не отказ пользователю и не
      // сессия: продолжить нельзя, повторить можно.
      return { kind: "transient-error", traceId: error.traceId };
    }
    // Прочие `4xx` здесь — неожиданность, а не состояние: пусть падает громко.
    throw error;
  }

  const configuration = new Configuration({
    basePath: API_BASE_PATH,
    credentials: CREDENTIALS,
    fetchApi,
    // `accessToken` намеренно не задан: его ставит `send`, и заголовок должен
    // собираться в одном месте, а не в двух — генератор поставил бы свой.
  });

  return {
    configuration,
    fetchApi,
    basePath: API_BASE_PATH,
    hasSession: () => accessToken !== null,
    setAccessToken: (token: string) => {
      accessToken = token;
    },
    markSessionEstablished: () => {
      sessionEstablished = true;
    },
    exchangeAuthorizationCode: (params) =>
      login(AUTH_CALLBACK_PATH, {
        code: params.code,
        code_verifier: params.codeVerifier,
        redirect_uri: params.redirectUri,
        device_id: params.deviceId,
      }),
    refreshAccessToken,
    bootstrap,
  };
}
