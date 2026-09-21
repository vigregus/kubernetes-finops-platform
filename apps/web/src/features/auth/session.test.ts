import { beforeEach, describe, expect, it } from "vitest";
import { installSubtleForJsdom } from "../../test-support/webcrypto";
import {
  CALLBACK_PATH,
  OIDC_CLIENT_ID,
  PENDING_LOGIN_KEY,
  beginLogin,
  clearPendingLogin,
  consumePendingLogin,
  redirectUri,
  startLogin,
} from "./session";

installSubtleForJsdom();

/** Тот же вектор RFC 7636, что и в `pkce.test.ts`: связка проверяется известным ответом. */
const RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
const RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

const ORIGIN = "https://app.finops.local";

describe("адрес входа", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("redirect_uri — origin плюс /callback, как в redirectUris реалма", () => {
    expect(redirectUri(ORIGIN)).toBe(`${ORIGIN}${CALLBACK_PATH}`);
  });

  it("уходит на Keycloak с S256 и тем самым верификатором, что остался в хранилище", async () => {
    const { authorizeUrl, pending } = await beginLogin({
      store: window.sessionStorage,
      origin: ORIGIN,
      createVerifier: () => RFC_VERIFIER,
      state: "state-fixed",
    });

    const url = new URL(authorizeUrl);
    expect(url.origin).toBe("https://idp.finops.local");
    expect(url.pathname).toBe("/realms/messenger/protocol/openid-connect/auth");
    expect(url.searchParams.get("client_id")).toBe(OIDC_CLIENT_ID);
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("scope")).toBe("openid");
    expect(url.searchParams.get("redirect_uri")).toBe(redirectUri(ORIGIN));
    expect(url.searchParams.get("state")).toBe("state-fixed");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    // Не самосогласие: ожидание взято из RFC 7636, Приложение B. Ценность в
    // том, что сверяется связка — в URL уехал вызов **того** верификатора,
    // который остался лежать в хранилище. Разошедшись, они дают отказ обмена
    // уже после того, как человек ввёл пароль.
    expect(url.searchParams.get("code_challenge")).toBe(RFC_CHALLENGE);
    expect(pending.codeVerifier).toBe(RFC_VERIFIER);
  });

  it("redirect_uri закодирован, а не склеен как есть", async () => {
    const { authorizeUrl } = await beginLogin({
      store: window.sessionStorage,
      origin: ORIGIN,
      createVerifier: () => RFC_VERIFIER,
    });
    // Иначе `&` и `?` внутри адреса разрезали бы строку запроса.
    expect(authorizeUrl).toContain("redirect_uri=https%3A%2F%2Fapp.finops.local%2Fcallback");
  });

  it("startLogin уводит браузер и оставляет незавершённый вход", async () => {
    const visited: string[] = [];
    const state = await startLogin({
      store: window.sessionStorage,
      origin: ORIGIN,
      navigate: (url) => visited.push(url),
    });
    expect(visited).toHaveLength(1);
    expect(visited[0]).toContain("protocol/openid-connect/auth");
    expect(consumePendingLogin(window.sessionStorage)?.state).toBe(state);
  });
});

describe("незавершённый вход", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("снимается при чтении: второй обмен по тому же коду невозможен", async () => {
    await beginLogin({
      store: window.sessionStorage,
      origin: ORIGIN,
      createVerifier: () => RFC_VERIFIER,
      state: "state-fixed",
    });

    expect(consumePendingLogin(window.sessionStorage)).toEqual({
      state: "state-fixed",
      codeVerifier: RFC_VERIFIER,
    });
    // Одноразовость — не «мы так думаем», а отсутствие ключа: верификатор не
    // переживает использование. Здесь же это ловит и повторный заход на
    // `/callback` с тем же `code`.
    expect(consumePendingLogin(window.sessionStorage)).toBeNull();
    expect(window.sessionStorage.getItem(PENDING_LOGIN_KEY)).toBeNull();
  });

  it("испорченное значение — «входа не было», и ключ всё равно снят", () => {
    window.sessionStorage.setItem(PENDING_LOGIN_KEY, "{не json");
    expect(consumePendingLogin(window.sessionStorage)).toBeNull();
    expect(window.sessionStorage.getItem(PENDING_LOGIN_KEY)).toBeNull();

    window.sessionStorage.setItem(PENDING_LOGIN_KEY, JSON.stringify({ state: "s" }));
    expect(consumePendingLogin(window.sessionStorage)).toBeNull();
  });

  it("clearPendingLogin убирает вход, не читая его", async () => {
    await beginLogin({ store: window.sessionStorage, origin: ORIGIN });
    clearPendingLogin(window.sessionStorage);
    expect(consumePendingLogin(window.sessionStorage)).toBeNull();
  });
});
