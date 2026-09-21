// Детекторы обёртки над клиентом (срез 2).
//
// Проверяются четыре инварианта, ради которых обёртка и существует:
// относительный `/api/v1`, `Authorization` и `X-Device-Id` в одном месте,
// различение `401` и `503`, и single-flight обмена токена.
//
// `fetch` подменяется на стаб, отвечающий **по пути**, а не по порядку: тест,
// зависящий от порядка запросов, краснеет от перестановки в `Promise.all`,
// то есть от того, чего он не проверяет.

import { describe, expect, it } from "vitest"

import {
  API_BASE_PATH,
  CREDENTIALS,
  DEVICE_ID_HEADER,
  createApiClient,
} from "./client"
import type { BootstrapDependencies } from "./client"
import { ConversationListPageFromJSON, MeFromJSON } from "./generated"
import {
  ApiProblem,
  ServiceUnavailableError,
  SessionExpiredError,
  UnauthenticatedError,
} from "./problems"

interface Call {
  readonly path: string
  readonly init: RequestInit
  readonly headers: Headers
}

type RouteResult = Response | Error | DOMException | Promise<Response | Error | DOMException>

function createStubFetch(routes: Record<string, readonly RouteResult[]>) {
  const calls: Call[] = []
  const queues = new Map(Object.entries(routes).map(([path, results]) => [path, [...results]]))

  const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url
    const path = url.split("?")[0]
    const call: Call = { path, init: init ?? {}, headers: new Headers(init?.headers) }
    calls.push(call)

    const queue = queues.get(path)
    if (queue === undefined || queue.length === 0) {
      throw new Error(`стаб не ждал запроса: ${path}`)
    }
    // Последний ответ повторяется: одна и та же ветка проверяется несколько раз.
    const result = queue.length === 1 ? queue[0] : (queue.shift() as RouteResult)
    const resolved = await result
    // Проверка идёт по `Response`, а не по `Error`: `DOMException` в этом
    // окружении не наследник `Error`, и проверка «наоборот» вернула бы отмену
    // как ответ — то есть тест упал бы на разборе тела, а не на своём предмете.
    if (resolved instanceof Response) {
      return resolved
    }
    throw resolved
  }

  return { calls, fetchImpl }
}

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/problem+json", ...headers },
  })
}

const ACCESS_TOKEN = { access_token: "token-1", expires_in: 300, device_id: "device-a" }

const PROBLEM_UNAUTHENTICATED = {
  type: "https://finops.local/problems/unauthenticated",
  title: "Вход не выполнен",
  status: 401,
  code: "UNAUTHENTICATED",
}

function depsOf(client: ReturnType<typeof createApiClient>): BootstrapDependencies {
  return {
    loadAccount: () => client.fetchApi(`${API_BASE_PATH}/me`, {}).then((response) => response.json()).then(MeFromJSON),
    loadConversations: () =>
      client
        .fetchApi(`${API_BASE_PATH}/conversations`, {})
        .then((response) => response.json())
        .then(ConversationListPageFromJSON),
  }
}

describe("базовый путь и учётные данные", () => {
  it("запрос уходит на собственный origin, а не на абсолютный адрес из servers", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    await client.refreshAccessToken()

    expect(stub.calls[0].path).toBe("/api/v1/auth/refresh")
    expect(stub.calls[0].path.startsWith("http")).toBe(false)
    expect(client.configuration.basePath).toBe("/api/v1")
  })

  it("учётные данные запрашиваются явно: same-origin, а не include", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    await client.refreshAccessToken()

    expect(stub.calls[0].init.credentials).toBe("same-origin")
    expect(CREDENTIALS).toBe("same-origin")
  })

  it("configuration не ставит accessToken сама: заголовок собирается в одном месте", () => {
    const client = createApiClient({ fetchImpl: (() => Promise.reject(new Error("unused"))) as never })

    expect(client.configuration.accessToken).toBeUndefined()
  })
})

describe("заголовки", () => {
  it("X-Device-Id уходит на обмене токена", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await client.refreshAccessToken()

    expect(stub.calls[0].headers.get(DEVICE_ID_HEADER)).toBe("device-a")
  })

  it("X-Device-Id уходит и на защищённом вызове", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/me`]: [jsonResponse(200, { user_id: "u1" })] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })
    client.setAccessToken("token-1")

    await client.fetchApi(`${API_BASE_PATH}/me`, {})

    expect(stub.calls[0].headers.get(DEVICE_ID_HEADER)).toBe("device-a")
  })

  it("refresh уходит без тела", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await client.refreshAccessToken()

    expect(stub.calls[0].init.body).toBeUndefined()
    expect(stub.calls[0].init.method).toBe("POST")
  })

  it("callback несёт device_id и телом, и заголовком", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/callback`]: [jsonResponse(200, ACCESS_TOKEN)] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await client.exchangeAuthorizationCode({
      code: "code-1",
      codeVerifier: "verifier-1",
      redirectUri: "https://app.finops.local/callback",
      deviceId: "device-a",
    })

    expect(stub.calls[0].headers.get(DEVICE_ID_HEADER)).toBe("device-a")
    expect(JSON.parse(String(stub.calls[0].init.body))).toEqual({
      code: "code-1",
      code_verifier: "verifier-1",
      redirect_uri: "https://app.finops.local/callback",
      device_id: "device-a",
    })
  })

  it("Authorization не ставится, пока токена нет", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    await client.refreshAccessToken()

    expect(stub.calls[0].headers.get("Authorization")).toBeNull()
  })

  it("Authorization ставится, когда токен есть", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/me`]: [jsonResponse(200, { user_id: "u1" })] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })
    client.setAccessToken("token-1")

    await client.fetchApi(`${API_BASE_PATH}/me`, {})

    expect(stub.calls[0].headers.get("Authorization")).toBe("Bearer token-1")
  })

  it("серверный device_id заменяет сохранённый, даже если отличается", async () => {
    const saved: string[] = []
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, { ...ACCESS_TOKEN, device_id: "device-b" })],
    })
    const client = createApiClient({
      fetchImpl: stub.fetchImpl as never,
      deviceId: () => "device-a",
      saveDeviceId: (deviceId) => saved.push(deviceId),
    })

    await client.refreshAccessToken()

    expect(saved).toEqual(["device-b"])
  })

  it("совпавший device_id не пересохраняется", async () => {
    const saved: string[] = []
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)] })
    const client = createApiClient({
      fetchImpl: stub.fetchImpl as never,
      deviceId: () => "device-a",
      saveDeviceId: (deviceId) => saved.push(deviceId),
    })

    await client.refreshAccessToken()

    expect(saved).toEqual([])
  })
})

describe("разбор отказа", () => {
  it("trace_id доезжает из тела", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(404, { type: "…/not-found", title: "Не найдено", status: 404, trace_id: "trace-body" }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })
    client.setAccessToken("token-1")

    const error = await client.fetchApi(`${API_BASE_PATH}/conversations`, {}).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiProblem)
    expect((error as ApiProblem).traceId).toBe("trace-body")
  })

  it("trace_id берётся из заголовка, когда тела нет", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/conversations`]: [
        new Response("<html>шлюз</html>", { status: 503, headers: { "X-Trace-Id": "trace-header" } }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })
    client.setAccessToken("token-1")

    const error = await client.fetchApi(`${API_BASE_PATH}/conversations`, {}).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ServiceUnavailableError)
    expect((error as ServiceUnavailableError).traceId).toBe("trace-header")
  })

  it("trace_id из тела важнее заголовка — правило одно, а не «как получится»", async () => {
    // Расхождения быть не должно: сервер кладёт одно значение и в тело, и в
    // заголовок. Но если middleware однажды сломается, у клиента обязана быть
    // определённая семантика, а не два равноправных источника. Правило названо
    // здесь затем, чтобы оно пережило первую же правку разбора.
    const stub = createStubFetch({
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(
          404,
          { type: "…/not-found", title: "Не найдено", status: 404, trace_id: "trace-body" },
          { "X-Trace-Id": "trace-header" },
        ),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })
    client.setAccessToken("token-1")

    const error = (await client.fetchApi(`${API_BASE_PATH}/conversations`, {}).catch((e: unknown) => e)) as ApiProblem

    expect(error.traceId).toBe("trace-body")
  })

  it("code читается из сырого тела, хотя в схеме Problem его нет", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(403, { type: "…/forbidden", title: "Запрещено", status: 403, code: "NOT_A_MEMBER" }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })
    client.setAccessToken("token-1")

    const error = (await client.fetchApi(`${API_BASE_PATH}/conversations`, {}).catch((e: unknown) => e)) as ApiProblem

    expect(error.code).toBe("NOT_A_MEMBER")
    expect(error.status).toBe(403)
  })

  it("отмена не выдаётся за недоступность сервиса", async () => {
    const abort = new DOMException("Aborted", "AbortError")
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [abort] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    const error = await client.refreshAccessToken().catch((e: unknown) => e)

    expect(error).not.toBeInstanceOf(ServiceUnavailableError)
    expect((error as Error).name).toBe("AbortError")
  })

  it("200 без access_token не выдаётся за успешный вход", async () => {
    const stub = createStubFetch({ [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, { expires_in: 300 })] })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    await expect(client.refreshAccessToken()).rejects.toThrow(/access_token/)
  })
})

describe("развилка 401 и 503", () => {
  it("стартовый 401 — это unauthenticated, а не истёкшая сессия", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(401, PROBLEM_UNAUTHENTICATED)],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    const state = await client.bootstrap(depsOf(client))

    expect(state).toEqual({ kind: "unauthenticated" })
  })

  it("401 от callback — это неудавшийся вход, а не истёкшая сессия", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/callback`]: [jsonResponse(401, PROBLEM_UNAUTHENTICATED)],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    const outcome = await client.exchangeAuthorizationCode({
      code: "code-1",
      codeVerifier: "verifier-1",
      redirectUri: "https://app.finops.local/callback",
      deviceId: "device-a",
    })

    expect(outcome).toEqual({ kind: "unauthenticated" })
  })

  it("503 от callback — транзитное состояние, а не кончившаяся сессия", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/callback`]: [jsonResponse(503, { title: "Сервис недоступен", status: 503 }, { "X-Trace-Id": "trace-503" })],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    const outcome = await client.exchangeAuthorizationCode({
      code: "code-1",
      codeVerifier: "verifier-1",
      redirectUri: "https://app.finops.local/callback",
      deviceId: "device-a",
    })

    expect(outcome).toEqual({ kind: "unavailable", traceId: "trace-503" })
    expect(client.hasSession()).toBe(false)
  })

  it("ready только после /me и /conversations, а не по успешному обмену", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)],
      [`${API_BASE_PATH}/me`]: [jsonResponse(200, { user_id: "u1" })],
      [`${API_BASE_PATH}/conversations`]: [jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null })],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    const state = await client.bootstrap(depsOf(client))

    expect(state).toMatchObject({ kind: "ready" })
  })

  it("ready несёт данные bootstrap, а не только факт готовности", async () => {
    // «Токен есть» и «клиент готов» — разные утверждения; но и «готов» без
    // данных не утверждение вовсе. Композиция, которой `ready` нужен, иначе
    // сходила бы за теми же данными второй раз — и получила бы второе понятие
    // готовности, способное разойтись с первым.
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)],
      [`${API_BASE_PATH}/me`]: [jsonResponse(200, { user_id: "u1", display_name: "David Miller" })],
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(200, {
          items: [{ conversation_id: "c1", type: "direct", participants: [], created_at: "2026-09-01T00:00:00Z" }],
          next_before_activity_at: null,
          next_before_conversation_id: null,
        }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    const state = await client.bootstrap(depsOf(client))

    expect(state.kind).toBe("ready")
    if (state.kind !== "ready") return

    // Идентификатор зрителя нужен адаптеру бесед: без него «собеседник»
    // неотличим от самого зрителя (B9г).
    expect(state.account.userId).toBe("u1")
    expect(state.conversations.items.map((conversation) => conversation.conversationId)).toEqual(["c1"])
  })

  it("503 на /me — транзитное состояние, ready не выставляется", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)],
      [`${API_BASE_PATH}/me`]: [jsonResponse(503, { title: "Сервис недоступен", status: 503 }, { "X-Trace-Id": "trace-me" })],
      [`${API_BASE_PATH}/conversations`]: [jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null })],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    const state = await client.bootstrap(depsOf(client))

    expect(state).toEqual({ kind: "transient-error", traceId: "trace-me" })
  })

  it("503 от защищённого маршрута не запускает обмен токена", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)],
      [`${API_BASE_PATH}/conversations`]: [jsonResponse(503, { title: "Сервис недоступен", status: 503 })],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })
    client.setAccessToken("token-1")

    const error = await client.fetchApi(`${API_BASE_PATH}/conversations`, {}).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ServiceUnavailableError)
    expect(stub.calls.filter((call) => call.path.endsWith("/auth/refresh"))).toHaveLength(0)
  })

  it("401 на обмене после ready — это истёкшая сессия", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN), jsonResponse(401, PROBLEM_UNAUTHENTICATED)],
      [`${API_BASE_PATH}/me`]: [
        jsonResponse(200, { user_id: "u1" }),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await expect(client.bootstrap(depsOf(client))).resolves.toMatchObject({ kind: "ready" })

    const error = await client.fetchApi(`${API_BASE_PATH}/me`, {}).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(SessionExpiredError)
    expect(error).not.toBeInstanceOf(UnauthenticatedError)
  })

  it("503 на обмене после ready — транзитное состояние, а не истёкшая сессия", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [
        jsonResponse(200, ACCESS_TOKEN),
        jsonResponse(503, { title: "Сервис недоступен", status: 503 }, { "X-Trace-Id": "trace-refresh" }),
      ],
      [`${API_BASE_PATH}/me`]: [
        jsonResponse(200, { user_id: "u1" }),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await expect(client.bootstrap(depsOf(client))).resolves.toMatchObject({ kind: "ready" })

    const error = await client.fetchApi(`${API_BASE_PATH}/me`, {}).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ServiceUnavailableError)
    expect((error as ServiceUnavailableError).traceId).toBe("trace-refresh")
    expect(error).not.toBeInstanceOf(SessionExpiredError)
  })

  it("503 на обмене снимает прежний токен, но не память о том, что сессия была", async () => {
    // Проверяется не «нет токена» и не «нет сессии», а их расхождение. До обмена
    // сервер отверг T ответом `401`; оставить T в памяти значило бы держать
    // `hasSession() === true` рядом с `transient-error` и отправить T снова —
    // токен, о котором заведомо известно, что он не работает.
    //
    // Вторая половина — обратная и не менее важная: `sessionEstablished`
    // остаётся истиной. «Валидного токена нет» и «сессии не было» — разные
    // факты, и следующий `401` на обмене обязан остаться «истекла», а не
    // превратиться в «первый визит».
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [
        jsonResponse(200, ACCESS_TOKEN),
        jsonResponse(503, { title: "Сервис недоступен", status: 503 }, { "X-Trace-Id": "trace-refresh" }),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/me`]: [
        jsonResponse(200, { user_id: "u1" }),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await expect(client.bootstrap(depsOf(client))).resolves.toMatchObject({ kind: "ready" })
    expect(client.hasSession()).toBe(true)

    const transient = await client.fetchApi(`${API_BASE_PATH}/me`, {}).catch((e: unknown) => e)

    expect(transient).toBeInstanceOf(ServiceUnavailableError)
    expect(client.hasSession()).toBe(false)

    const expired = await client.fetchApi(`${API_BASE_PATH}/me`, {}).catch((e: unknown) => e)

    expect(expired).toBeInstanceOf(SessionExpiredError)
    expect(expired).not.toBeInstanceOf(UnauthenticatedError)

    // Токен не просто снят со счёта — он не ушёл по проводу: третий вызов уходит
    // без заголовка, и это наблюдаемо, а не выводится из `hasSession()`.
    const meCalls = stub.calls.filter((call) => call.path === `${API_BASE_PATH}/me`)
    expect(meCalls).toHaveLength(3)
    expect(meCalls[2].headers.get("Authorization")).toBeNull()
  })

  it("после транзитного отказа законный 401 ведёт в unauthenticated, а не в «истекла»", async () => {
    // Сервер снимает refresh-cookie при любом неудачном обмене, включая 503
    // (`api/main.py`). Поэтому повтор после транзитного отказа может вернуть
    // 401 — и это не ошибка клиента, а серверная семантика, принятая явно,
    // а не обойдённая: подразумевать, что cookie пережила отказ, нельзя.
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [
        jsonResponse(200, ACCESS_TOKEN),
        jsonResponse(503, { title: "Сервис недоступен", status: 503 }),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/me`]: [jsonResponse(200, { user_id: "u1" })],
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await expect(client.bootstrap(depsOf(client))).resolves.toMatchObject({ kind: "ready" })
    await expect(client.bootstrap(depsOf(client))).resolves.toEqual({
      kind: "transient-error",
      traceId: undefined,
    })
    await expect(client.bootstrap(depsOf(client))).resolves.toEqual({ kind: "unauthenticated" })

    expect(client.hasSession()).toBe(false)
  })
})

describe("single-flight обмена токена", () => {
  it("два одновременных 401 дают один обмен", async () => {
    const calls: string[] = []
    let refreshCount = 0

    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      void init
      const path = (typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url).split("?")[0]
      calls.push(path)

      if (path === `${API_BASE_PATH}/auth/refresh`) {
        refreshCount += 1
        return jsonResponse(200, ACCESS_TOKEN)
      }
      const seen = calls.filter((seenPath) => seenPath === path).length
      if (seen === 1) {
        // Первый заход каждого запроса — без токена, то есть 401.
        return jsonResponse(401, PROBLEM_UNAUTHENTICATED)
      }
      return path.endsWith("/conversations")
        ? jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null })
        : jsonResponse(200, { user_id: "u1" })
    }

    const client = createApiClient({ fetchImpl: fetchImpl as never, deviceId: () => "device-a" })

    await Promise.all([
      client.fetchApi(`${API_BASE_PATH}/me`, {}),
      client.fetchApi(`${API_BASE_PATH}/conversations`, {}),
    ])

    expect(refreshCount).toBe(1)
    expect(calls.filter((path) => path.endsWith("/auth/refresh"))).toHaveLength(1)
  })
})

describe("повтор после обмена", () => {
  it("второй 401 после успешного обмена не запускает обмен заново", async () => {
    // Single-flight отвечает только про одновременные вызовы. Здесь вызовы
    // последовательные, и защита нужна другая: запрос → 401 → обмен → повтор →
    // снова 401. Если повтор пойдёт тем же общим путём, что и первый заход,
    // выйдет обмен на каждый `401` — петля, в которой вращающаяся cookie
    // сжигается до конца, а человек видит не отказ, а зависание.
    //
    // Числа здесь и есть утверждение: обмен ровно один на исходный запрос,
    // попыток запроса ровно две, дальше — потеря сессии.
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [
        jsonResponse(200, ACCESS_TOKEN),
        jsonResponse(200, ACCESS_TOKEN),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/me`]: [
        jsonResponse(200, { user_id: "u1" }),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/conversations`]: [
        jsonResponse(200, { items: [], next_before_activity_at: null, next_before_conversation_id: null }),
      ],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never, deviceId: () => "device-a" })

    await expect(client.bootstrap(depsOf(client))).resolves.toMatchObject({ kind: "ready" })

    const before = stub.calls.length
    const error = await client.fetchApi(`${API_BASE_PATH}/me`, {}).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(SessionExpiredError)

    const since = stub.calls.slice(before)
    expect(since.filter((call) => call.path.endsWith("/auth/refresh"))).toHaveLength(1)
    expect(since.filter((call) => call.path === `${API_BASE_PATH}/me`)).toHaveLength(2)
  })
})
