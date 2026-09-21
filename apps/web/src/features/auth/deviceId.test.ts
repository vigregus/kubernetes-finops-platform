import { beforeEach, describe, expect, it } from "vitest";
import { createApiClient } from "../../api/client";
import { ConversationListPageFromJSON, MeFromJSON, type FetchAPI } from "../../api/generated";
import { installStorageForJsdom } from "../../test-support/storage";
import { DEVICE_ID_KEY, ensureDeviceId, loadDeviceId, saveDeviceId } from "./deviceId";

// jsdom не отдаёт `localStorage` (замер — в `test-support/storage.ts`).
installStorageForJsdom();

/**
 * Здесь работает **настоящая обёртка** и хранилище целиком, а не заглушки.
 *
 * Разница существенная: `client.test.ts` проверяет, что обёртка *вызвала*
 * `saveDeviceId`, — и это утверждение о вызове. Здесь проверяется, что после
 * ответа сервера в хранилище лежит именно серверный идентификатор и **следующий**
 * запрос уходит уже с ним. Вызов можно сделать и забыть применить; хранилище
 * показывает, что применили.
 */

interface Call {
  readonly path: string;
  readonly method: string;
  readonly headers: Headers;
}

type Route = () => Response;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function createStubFetch(routes: Record<string, Route>): {
  readonly fetchImpl: FetchAPI;
  readonly calls: Call[];
} {
  const calls: Call[] = [];
  const fetchImpl: FetchAPI = async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const path = new URL(url, "https://app.finops.local").pathname;
    calls.push({
      path,
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
    });
    const route = routes[path];
    if (route === undefined) {
      throw new Error(`стаб не ждал запроса: ${path}`);
    }
    return route();
  };
  return { fetchImpl, calls };
}

/** Обёртка, подключённая к настоящему `localStorage` — как в `main.tsx`. */
function createClient(fetchImpl: FetchAPI) {
  return createApiClient({
    fetchImpl,
    deviceId: () => loadDeviceId(window.localStorage),
    saveDeviceId: (deviceId) => {
      saveDeviceId(window.localStorage, deviceId);
    },
  });
}

const BOOTSTRAP_OK: Record<string, Route> = {
  "/api/v1/auth/refresh": () => jsonResponse(200, { access_token: "token-1", device_id: "device-a" }),
  "/api/v1/me": () => jsonResponse(200, { user_id: "user-1", display_name: "Ann" }),
  "/api/v1/conversations": () => jsonResponse(200, { items: [] }),
};

/**
 * Данные загрузки — настоящие DTO, собранные конвертерами.
 *
 * `ready` наступает только вместе с ответами `/me` и `/conversations`
 * (B9а), поэтому загрузка обязана их **отдать**: заглушка, возвращающая
 * `void`, проверяла бы обёртку, которой нечего сообщить о готовности.
 * Собираются конвертерами, а не литералами, — по проводу, в snake_case.
 */
const ACCOUNT = MeFromJSON({
  user_id: "user-1",
  display_name: "Ann",
  email: "ann@example.com",
  email_verified: true,
  capabilities: ["read"],
});
const CONVERSATIONS = ConversationListPageFromJSON({
  items: [],
  next_before_activity_at: null,
  next_before_conversation_id: null,
});
const bootstrapDeps = {
  loadAccount: () => Promise.resolve(ACCOUNT),
  loadConversations: () => Promise.resolve(CONVERSATIONS),
};

describe("хранилище идентификатора устройства", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("пустое хранилище: заводится ровно один ключ, и он же возвращается", () => {
    const created = ensureDeviceId(window.localStorage, () => "device-created");
    expect(created).toBe("device-created");
    expect(Object.keys(window.localStorage)).toEqual([DEVICE_ID_KEY]);
    expect(loadDeviceId(window.localStorage)).toBe("device-created");
  });

  it("занятое хранилище не перезаписывается: второй вход предъявляет тот же идентификатор", () => {
    saveDeviceId(window.localStorage, "device-existing");
    // Генератор, который обязан остаться невызванным: если бы идентификатор
    // заводился заново, каждое открытие вкладки добавляло бы строку в `devices`.
    expect(ensureDeviceId(window.localStorage, () => "device-NEW")).toBe("device-existing");
    expect(loadDeviceId(window.localStorage)).toBe("device-existing");
  });

  it("пустая строка — не идентификатор: сервер завёл бы новое устройство", () => {
    window.localStorage.setItem(DEVICE_ID_KEY, "");
    expect(loadDeviceId(window.localStorage)).toBeNull();
    expect(ensureDeviceId(window.localStorage, () => "device-created")).toBe("device-created");
  });

  it("идентификатор заведён до первого запроса: refresh уже несёт X-Device-Id", async () => {
    const stub = createStubFetch(BOOTSTRAP_OK);
    const client = createClient(stub.fetchImpl);

    // Порядок как в `main.tsx`: сначала идентификатор, потом первый запрос.
    const deviceId = ensureDeviceId(window.localStorage, () => "device-created");
    await client.bootstrap(bootstrapDeps);

    expect(stub.calls[0]?.path).toBe("/api/v1/auth/refresh");
    expect(stub.calls[0]?.headers.get("X-Device-Id")).toBe(deviceId);
    // Первый запрос — refresh, и тела у него нет: в теле идентификатор ехать не может.
    expect(window.localStorage.length).toBe(1);
  });

  it("в хранилище ровно один ключ и после входа, и после refresh", async () => {
    const stub = createStubFetch(BOOTSTRAP_OK);
    const client = createClient(stub.fetchImpl);
    ensureDeviceId(window.localStorage, () => "device-a");

    await client.bootstrap(bootstrapDeps);
    await client.refreshAccessToken();

    expect(Object.keys(window.localStorage)).toEqual([DEVICE_ID_KEY]);
    // Токена здесь нет и быть не может: `ADR 0005` — «localStorage для
    // удостоверений не используется никогда». Токен живёт в замыкании вкладки.
    expect(Object.values(window.localStorage)).not.toContain("token-1");
  });

  it("серверный идентификатор становится хранимым, и следующий вызов уходит уже с ним", async () => {
    const stub = createStubFetch({
      ...BOOTSTRAP_OK,
      // Занятый идентификатор сервер не отвергает ошибкой, а заменяет новым —
      // так выглядит вход второго человека в том же браузере.
      "/api/v1/auth/refresh": () =>
        jsonResponse(200, { access_token: "token-2", device_id: "device-server" }),
      "/api/v1/me/after": () => jsonResponse(200, {}),
    });
    const client = createClient(stub.fetchImpl);
    ensureDeviceId(window.localStorage, () => "device-stale");

    await client.bootstrap(bootstrapDeps);
    expect(loadDeviceId(window.localStorage)).toBe("device-server");

    await client.fetchApi("/api/v1/me/after");
    const last = stub.calls.at(-1);
    expect(last?.headers.get("X-Device-Id")).toBe("device-server");
  });
});
