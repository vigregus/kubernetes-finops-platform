import { beforeEach, describe, expect, it } from "vitest";
import type { ApiClient, LoginOutcome } from "../../api/client";
import { installSubtleForJsdom } from "../../test-support/webcrypto";
import { type CallbackOutcome, bootStateOf, completeLogin } from "./callback";
import { OIDC_ISSUER, PENDING_LOGIN_KEY, beginLogin, consumePendingLogin } from "./session";

installSubtleForJsdom();

const ORIGIN = "https://app.finops.local";
const RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";

type ExchangeParams = Parameters<ApiClient["exchangeAuthorizationCode"]>[0];

interface Stub {
  readonly client: Pick<ApiClient, "exchangeAuthorizationCode">;
  readonly calls: ExchangeParams[];
}

function stubClient(outcome: LoginOutcome): Stub {
  const calls: ExchangeParams[] = [];
  return {
    calls,
    client: {
      exchangeAuthorizationCode: (params) => {
        calls.push(params);
        return Promise.resolve(outcome);
      },
    },
  };
}

/** Кладёт незавершённый вход так, как это делает Keycloak-редирект. */
async function givenPendingLogin(state = "state-fixed"): Promise<void> {
  await beginLogin({
    store: window.sessionStorage,
    origin: ORIGIN,
    issuer: OIDC_ISSUER,
    createVerifier: () => RFC_VERIFIER,
    state,
  });
}

function withoutExchange(stub: Stub): void {
  expect(stub.calls).toHaveLength(0);
}

/**
 * `bootStateOf` для неуспешного исхода. Успех состоянием не описывается — на нём
 * загрузка продолжается, — и `null` здесь честнее выдуманного состояния. Без
 * этого помощника пришлось бы сужать тип приведением, то есть утверждать то,
 * что тест как раз и проверяет.
 */
function bootStateOfOrNull(outcome: CallbackOutcome): ReturnType<typeof bootStateOf> | null {
  return outcome.kind === "ok" ? null : bootStateOf(outcome);
}

describe("исходы callback", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("200: код обменян, устройство названо сервером", async () => {
    const stub = stubClient({ kind: "ok", deviceId: "device-server" });
    await givenPendingLogin();

    const outcome = await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-1&state=state-fixed",
      deviceId: "device-stored",
    });

    expect(outcome).toEqual({ kind: "ok", deviceId: "device-server" });
    expect(stub.calls).toHaveLength(1);
    expect(stub.calls[0]).toEqual({
      code: "code-1",
      codeVerifier: RFC_VERIFIER,
      redirectUri: `${ORIGIN}/callback`,
      deviceId: "device-stored",
    });
  });

  it("401: вход не удался — и это не «сессия истекла»", async () => {
    const stub = stubClient({ kind: "unauthenticated" });
    await givenPendingLogin();

    const outcome = await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-1&state=state-fixed",
      deviceId: null,
    });

    expect(outcome).toEqual({ kind: "unauthenticated" });
    // Отказа со стороны сервера недостаточно, чтобы утверждать об истечении:
    // сессии не было вовсе — вход не состоялся.
    expect(bootStateOfOrNull(outcome)).toEqual({ kind: "unauthenticated" });
  });

  it("503: транзитное состояние с трассой, без ухода в Keycloak", async () => {
    const stub = stubClient({ kind: "unavailable", traceId: "trace-503" });
    await givenPendingLogin();

    const outcome = await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-1&state=state-fixed",
      deviceId: null,
    });

    expect(outcome).toEqual({ kind: "transient-error", traceId: "trace-503" });
    // Ни «истекла», ни повторного редиректа: обмен обрабатывается тем же
    // `_login_failure`, что и refresh, и `503` там означает «повтори позже».
    expect(bootStateOfOrNull(outcome)).toEqual({
      kind: "transient-error",
      traceId: "trace-503",
    });
  });
});

describe("отказы до обращения к серверу", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("чужой state: обмена не происходит вовсе", async () => {
    const stub = stubClient({ kind: "ok", deviceId: "device-server" });
    await givenPendingLogin("state-ours");

    const outcome = await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-attacker&state=state-theirs",
      deviceId: null,
    });

    expect(outcome).toEqual({ kind: "unauthenticated", refusal: "state-mismatch" });
    // Несущее утверждение: чужой код не уходит на сервер. Обмен «на всякий
    // случай, а сверим потом» оставил бы единственной защитой `code_verifier`,
    // то есть ровно то, что `state` и бережёт, — привязку кода к браузеру.
    withoutExchange(stub);
    // И незавершённый вход снят: держать его после отказа нечего.
    expect(window.sessionStorage.getItem(PENDING_LOGIN_KEY)).toBeNull();
  });

  it("возврат без начатого входа", async () => {
    const stub = stubClient({ kind: "ok", deviceId: "device-server" });

    const outcome = await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-1&state=state-fixed",
      deviceId: null,
    });

    expect(outcome).toEqual({ kind: "unauthenticated", refusal: "no-pending-login" });
    withoutExchange(stub);
  });

  it("Keycloak вернул не то, что обещал", async () => {
    const stub = stubClient({ kind: "ok", deviceId: "device-server" });
    await givenPendingLogin();

    for (const search of ["", "?state=state-fixed", "?code=code-1", "?code=&state=state-fixed"]) {
      const outcome = await completeLogin({
        client: stub.client,
        store: window.sessionStorage,
        origin: ORIGIN,
        search,
        deviceId: null,
      });
      expect(outcome).toEqual({ kind: "unauthenticated", refusal: "missing-params" });
    }
    withoutExchange(stub);
  });

  it("повторный заход на /callback ничего не обменивает", async () => {
    const stub = stubClient({ kind: "ok", deviceId: "device-server" });
    await givenPendingLogin();

    const first = await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-1&state=state-fixed",
      deviceId: null,
    });
    expect(first).toEqual({ kind: "ok", deviceId: "device-server" });

    // Перезагрузка страницы по тому же адресу: `code` в истории браузера
    // остался, но обменять его второй раз нельзя — verifier уже снят.
    const second = await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-1&state=state-fixed",
      deviceId: null,
    });
    expect(second).toEqual({ kind: "unauthenticated", refusal: "no-pending-login" });
    expect(stub.calls).toHaveLength(1);
  });

  it("незавершённый вход снимается и при неудачном обмене", async () => {
    const stub = stubClient({ kind: "unavailable", traceId: "trace-503" });
    await givenPendingLogin();

    await completeLogin({
      client: stub.client,
      store: window.sessionStorage,
      origin: ORIGIN,
      search: "?code=code-1&state=state-fixed",
      deviceId: null,
    });

    expect(consumePendingLogin(window.sessionStorage)).toBeNull();
  });
});
