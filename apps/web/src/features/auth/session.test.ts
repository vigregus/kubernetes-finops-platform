import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { RuntimeConfigMissingError } from "../../runtime-config";
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
import type { PendingLoginStore } from "./session";

installSubtleForJsdom();

/** Тот же вектор RFC 7636, что и в `pkce.test.ts`: связка проверяется известным ответом. */
const RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
const RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

const ORIGIN = "https://app.finops.local";

/**
 * Источник идентичности в тесте — свой, а не из окружения приложения.
 *
 * Этот файл проверяет, что уезжает в `code_challenge` и что остаётся лежать до
 * возврата, а не то, откуда берётся имя IdP. `beginLogin` его и не читает —
 * адрес ему передают явно.
 */
const ISSUER = "https://idp.finops.local/realms/messenger";

/**
 * Окружение, каким его видит вход.
 *
 * `startLogin` читает источник идентичности сам — это единственная точка, где
 * приложение вообще касается окружения, — поэтому тесты входа обязаны его
 * предъявить. Именно предъявить: тест, оставшийся без конфигурации, обязан
 * упасть по имени, а не найти вшитое значение. Обратное — снятие — делает
 * `afterEach`, иначе отсутствие конфигурации зеленело бы от соседнего теста.
 */
function givenRuntimeConfig(issuer = ISSUER): void {
  window.__MESSENGER_RUNTIME_CONFIG__ = { oidcIssuer: issuer };
}

// На уровне файла, а не внутри `describe`: отсутствие конфигурации — состояние,
// которое тест обязан предъявить сам, а не унаследовать от порядка прогона.
afterEach(() => {
  delete window.__MESSENGER_RUNTIME_CONFIG__;
});

/**
 * Хранилище со счётчиком записей. «Вход не начат» — утверждение о числе
 * записей, и читать его надо числом: неудачная попытка, дошедшая до записи
 * `pending`, оставляет в хранилище ровно то же значение, что и удачная, и по
 * содержимому они неразличимы.
 */
function countingStore(inner: Storage): PendingLoginStore & { readonly writes: number } {
  let writes = 0;
  return {
    get writes() {
      return writes;
    },
    getItem: (key) => inner.getItem(key),
    setItem: (key, value) => {
      writes += 1;
      inner.setItem(key, value);
    },
    removeItem: (key) => inner.removeItem(key),
  };
}

describe("адрес входа", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    givenRuntimeConfig();
  });

  it("redirect_uri — origin плюс /callback, как в redirectUris реалма", () => {
    expect(redirectUri(ORIGIN)).toBe(`${ORIGIN}${CALLBACK_PATH}`);
  });

  it("уходит на Keycloak с S256 и тем самым верификатором, что остался в хранилище", async () => {
    const { authorizeUrl, pending } = await beginLogin({
      store: window.sessionStorage,
      origin: ORIGIN,
      issuer: ISSUER,
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
      issuer: ISSUER,
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

describe("вход без конфигурации окружения", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    // Конфигурации нет — это и есть условие описываемого случая. Ставится
    // явно, а не подразумевается отсутствием настройки в `beforeEach`:
    // «не настроено» и «настроено, но не тем кодом» — разные состояния, и
    // второе обязано быть видно в тексте теста.
    delete window.__MESSENGER_RUNTIME_CONFIG__;
  });

  it("вход не начинается, а отказ назван по имени", async () => {
    const store = countingStore(window.sessionStorage);
    const visited: string[] = [];

    const refusal: unknown = await startLogin({
      store,
      origin: ORIGIN,
      navigate: (url) => visited.push(url),
    }).then(
      () => null,
      (error: unknown) => error,
    );

    // По имени, а не «что-то пошло не так»: чинить надо развёртывание, и
    // разбирающий обязан увидеть это из самого исключения. `Error` с текстом
    // здесь недостаточно — текст читают глазами, а имя различает ветку.
    expect(refusal).toBeInstanceOf(RuntimeConfigMissingError);
    expect((refusal as Error).name).toBe("RuntimeConfigMissingError");

    // И вход действительно не начат. Оба числа несущие, и вот почему:
    // значение по умолчанию, вшитое в бандл, этот тест **прошло бы** по
    // первому утверждению, если бы падало где-то дальше, — а по этим двум
    // нет. Вшитое имя IdP уводит браузер на источник, которого стенд не
    // объявлял: человек увидел бы чужой экран входа, а `state` и `verifier`
    // остались бы в хранилище от входа, который никуда не привёл.
    expect(store.writes).toBe(0);
    expect(visited).toHaveLength(0);
  });

  it("неудачная попытка не запирает вход: следующая с конфигурацией проходит", async () => {
    const store = countingStore(window.sessionStorage);
    const visited: string[] = [];

    await startLogin({ store, origin: ORIGIN, navigate: (url) => visited.push(url) }).catch(
      () => undefined,
    );

    // Отказ остался в прошлом: он не должен превратиться в «кнопка замолчала
    // навсегда». Повтор после неудачи — ровно то место, где такая ошибка была
    // бы видна.
    givenRuntimeConfig();
    const state = await startLogin({
      store,
      origin: ORIGIN,
      navigate: (url) => visited.push(url),
    });

    expect(visited).toHaveLength(1);
    expect(consumePendingLogin(store)?.state).toBe(state);
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
      issuer: ISSUER,
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
    await beginLogin({ store: window.sessionStorage, origin: ORIGIN, issuer: ISSUER });
    clearPendingLogin(window.sessionStorage);
    expect(consumePendingLogin(window.sessionStorage)).toBeNull();
  });
});
