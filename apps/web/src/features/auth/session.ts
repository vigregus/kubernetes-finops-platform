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
import { createCodeChallenge, createCodeVerifier, createState } from "./pkce";

/** Источник идентичности стенда. Значение — из `realm.yaml`, не из документации. */
export const OIDC_ISSUER = "https://idp.finops.local/realms/messenger";

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
  readonly issuer?: string;
  readonly clientId?: string;
  readonly createVerifier?: () => string;
  readonly state?: string;
}): Promise<{ readonly authorizeUrl: string; readonly pending: PendingLogin }> {
  const codeVerifier = (params.createVerifier ?? createCodeVerifier)();
  const state = params.state ?? createState();
  const pending: PendingLogin = { state, codeVerifier };
  params.store.setItem(PENDING_LOGIN_KEY, JSON.stringify(pending));

  const authorizeUrl = buildAuthorizeUrl({
    issuer: params.issuer ?? OIDC_ISSUER,
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

/** Настоящий вход: заводит незавершённый вход и уводит браузер на Keycloak. */
export async function startLogin(params: {
  readonly store: PendingLoginStore;
  readonly origin: string;
  readonly navigate?: (url: string) => void;
}): Promise<string> {
  const { authorizeUrl, pending } = await beginLogin({ store: params.store, origin: params.origin });
  (params.navigate ?? ((url: string) => window.location.assign(url)))(authorizeUrl);
  return pending.state;
}
