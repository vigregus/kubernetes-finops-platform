/**
 * Параметры входа и «незавершённый вход».
 *
 * `code_verifier` и `state` живут в `sessionStorage` ровно до одного обмена.
 * Это не удостоверение: верификатор — одноразовый секрет на время одного
 * редиректа, и после обмена он бесполезен. Именно поэтому он здесь, а не в
 * памяти модуля (перезагрузка на `/callback` потеряла бы его, и обмен не
 * состоялся бы вовсе) и не в `localStorage` (там он пережил бы вкладку, а
 * `ADR 0005` держит `localStorage` под один `device_id`).
 */
import { loadRuntimeConfig } from "../../runtime-config";
import { createCodeChallenge, createCodeVerifier, createState } from "./pkce";

// Источника идентичности в бандле нет — и это не упущение, а требование:
// вшитое имя IdP означало бы отдельную сборку на каждый стенд, тогда как
// `DEP-001` обещает один образ на все. Имя приходит из окружения — файлом
// `/runtime-config.js` рядом с `index.html` (см. `runtime-config.ts`).
//
// Прежнее значение бралось из `realm.yaml` стенда, и бралось верно; вопрос был
// не в нём, а в том, что оно вообще попадало в сборку.

/** Публичный клиент с PKCE S256 и `directAccessGrantsEnabled: false`. */
export const OIDC_CLIENT_ID = "messenger-web";

/** `openid` достаточно: профиль приходит из `/me`, а не из id-токена. */
export const OIDC_SCOPE = "openid";

/**
 * Путь возврата. Совпадает с `redirectUris` реалма — `https://app.finops.local/*`,
 * поэтому реалм не правится: `/callback` уже покрыт.
 */
export const CALLBACK_PATH = "/callback";

/** Ключ незавершённого входа. Один — как и `device_id`: второе значение означало бы вторую правду. */
export const PENDING_LOGIN_KEY = "pkce_pending";

/** Что нужно от `sessionStorage`. `removeItem` здесь несущий, а не служебный. */
export interface PendingLoginStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** Незавершённый вход: то, что предъявляется при возврате из Keycloak. */
export interface PendingLogin {
  readonly state: string;
  readonly codeVerifier: string;
}

/** `redirect_uri` строится от origin, а не берётся константой: он обязан совпасть с тем, откуда ушли. */
export function redirectUri(origin: string): string {
  return `${origin}${CALLBACK_PATH}`;
}

/** Адрес страницы входа Keycloak. Порядок параметров значения не имеет, набор — имеет. */
export function buildAuthorizeUrl(params: {
  readonly issuer: string;
  readonly clientId: string;
  readonly redirectUri: string;
  readonly state: string;
  readonly codeChallenge: string;
}): string {
  const query = new URLSearchParams({
    client_id: params.clientId,
    response_type: "code",
    scope: OIDC_SCOPE,
    redirect_uri: params.redirectUri,
    state: params.state,
    code_challenge: params.codeChallenge,
    code_challenge_method: "S256",
  });
  return `${params.issuer}/protocol/openid-connect/auth?${query.toString()}`;
}

/** Разбор сохранённого. Испорченное значение — не падение, а «входа не было». */
function parsePendingLogin(raw: string | null): PendingLogin | null {
  if (raw === null || raw === "") {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) {
      return null;
    }
    const { state, codeVerifier } = parsed as Partial<PendingLogin>;
    if (
      typeof state !== "string" ||
      state === "" ||
      typeof codeVerifier !== "string" ||
      codeVerifier === ""
    ) {
      return null;
    }
    return { state, codeVerifier };
  } catch {
    return null;
  }
}

/**
 * Начинает вход: заводит `state` и `code_verifier`, кладёт их в хранилище и
 * возвращает адрес, на который надо уйти.
 *
 * Возвращает адрес, а не уводит сам: увод — это `location.assign`, которого в
 * unit-тесте нет, а проверять надо именно то, что уедет в `code_challenge` и
 * что останется лежать до возврата.
 */
export async function beginLogin(params: {
  readonly store: PendingLoginStore;
  readonly origin: string;
  /**
   * Обязателен, а не взят из окружения здесь же: источник идентичности —
   * вход этой функции, а не глобальная переменная модуля. Иначе у неё
   * появилась бы зависимость, которой не видно в подписи, и тест «уходит на
   * Keycloak» перестал бы называть то, что проверяет.
   */
  readonly issuer: string;
  readonly clientId?: string;
  readonly createVerifier?: () => string;
  readonly state?: string;
}): Promise<{ readonly authorizeUrl: string; readonly pending: PendingLogin }> {
  const codeVerifier = (params.createVerifier ?? createCodeVerifier)();
  const state = params.state ?? createState();
  const pending: PendingLogin = { state, codeVerifier };
  params.store.setItem(PENDING_LOGIN_KEY, JSON.stringify(pending));

  const authorizeUrl = buildAuthorizeUrl({
    issuer: params.issuer,
    clientId: params.clientId ?? OIDC_CLIENT_ID,
    redirectUri: redirectUri(params.origin),
    state,
    codeChallenge: await createCodeChallenge(codeVerifier),
  });
  return { authorizeUrl, pending };
}

/**
 * Забирает незавершённый вход **и снимает его**.
 *
 * Снятие — часть чтения, а не следующий шаг: разделённые, они оставляют
 * `code_verifier` в хранилище, если между ними что-то упало, — и он переживает
 * использование, чего одноразовый секрет делать не должен. Второй вызов
 * обязан вернуть `null`; это и есть проверка одноразовости.
 */
export function consumePendingLogin(store: PendingLoginStore): PendingLogin | null {
  const pending = parsePendingLogin(store.getItem(PENDING_LOGIN_KEY));
  store.removeItem(PENDING_LOGIN_KEY);
  return pending;
}

/** Убирает незавершённый вход, не читая его. Нужен там, где обмен не состоится. */
export function clearPendingLogin(store: PendingLoginStore): void {
  store.removeItem(PENDING_LOGIN_KEY);
}

/**
 * Один начатый вход на вкладку.
 *
 * Между записью `pending` в хранилище и уводом браузера стоит `await` — на
 * `crypto.subtle.digest`. Второй клик успевает записать **свой** `pending`
 * раньше, чем первый увёл браузер, и входов оказывается два вместо одного: в
 * хранилище остаётся `state` второго, а браузер уходит по адресу первого.
 * Вернувшийся из Keycloak `state` сверяется тогда с чужим `pending`, и обмен
 * отвергается как чужой — вход не удаётся по причине, которой человек не делал.
 *
 * Разделяемый промис, а не запрет кнопки: запрет — состояние представления, и
 * держать его пришлось бы в компоненте, который о входах ничего не знает, а
 * второй клик тогда просто не имел бы обработчика. Обмен токена устроен так же
 * (`refreshInFlight` в `api/client.ts`) и по той же причине.
 */
let loginInFlight: Promise<string> | null = null;

/** Настоящий вход: заводит незавершённый вход и уводит браузер на Keycloak. */
export async function startLogin(params: {
  readonly store: PendingLoginStore;
  readonly origin: string;
  readonly navigate?: (url: string) => void;
}): Promise<string> {
  if (loginInFlight === null) {
    // Обнуление — в `finally`, а не по завершении: вход, который никуда не увёл
    // (увод подменён, `location.assign` не сработал), обязан оставить
    // возможность повторить, иначе кнопка замолчит навсегда.
    loginInFlight = beginAndNavigate(params).finally(() => {
      loginInFlight = null;
    });
  }
  return loginInFlight;
}

async function beginAndNavigate(params: {
  readonly store: PendingLoginStore;
  readonly origin: string;
  readonly navigate?: (url: string) => void;
}): Promise<string> {
  // Окружение читается здесь — в единственном месте, откуда вход начинается.
  // Отказ (`RuntimeConfigMissingError`) уходит наверх отклонением: чинить надо
  // развёртывание, а не повторять нажатие.
  const { authorizeUrl, pending } = await beginLogin({
    store: params.store,
    origin: params.origin,
    issuer: loadRuntimeConfig().oidcIssuer,
  });
  (params.navigate ?? ((url: string) => window.location.assign(url)))(authorizeUrl);
  return pending.state;
}
