// Детектор дефекта: без withUnwrappedErrors потерянная сессия на
// /realtime/token уходит наружу как FetchError генератора, а не как
// SessionExpiredError/UnauthenticatedError — и getData в realtimeClient.ts
// её не узнаёт (см. комментарий у createRealtimeTicketIssuer). Живой дефект
// верификации §18: WS-реконнект ретраил POST /realtime/token бесконечно,
// потому что фикс из connectionMachine.ts/realtimeClient.ts никогда не
// видел нужный тип ошибки.

import { describe, expect, it } from "vitest"

import { API_BASE_PATH, createApiClient } from "./client"
import type { BootstrapDependencies } from "./client"
import { createRealtimeTicketIssuer } from "./realtimeToken"
import { SessionExpiredError, UnauthenticatedError } from "./problems"
import { FetchError } from "./generated"

interface Call {
  readonly path: string
}

function createStubFetch(routes: Record<string, readonly Response[]>) {
  const queues = new Map(Object.entries(routes).map(([path, results]) => [path, [...results]]))
  const calls: Call[] = []

  const fetchImpl = async (input: RequestInfo | URL): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url
    const path = url.split("?")[0]
    calls.push({ path })

    const queue = queues.get(path)
    if (queue === undefined || queue.length === 0) {
      throw new Error(`стаб не ждал запроса: ${path}`)
    }
    return queue.length === 1 ? queue[0] : (queue.shift() as Response)
  }

  return { calls, fetchImpl }
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
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
    loadAccount: () => client.fetchApi(`${API_BASE_PATH}/me`, {}).then((r) => r.json()),
    loadConversations: () => client.fetchApi(`${API_BASE_PATH}/conversations`, {}).then((r) => r.json()),
  }
}

describe("createRealtimeTicketIssuer: потерянная сессия узнаётся вызывающим", () => {
  it("issueTicket() отклоняется SessionExpiredError, а не FetchError генератора — сессия была установлена", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [
        jsonResponse(200, ACCESS_TOKEN),
        jsonResponse(401, PROBLEM_UNAUTHENTICATED),
      ],
      [`${API_BASE_PATH}/me`]: [jsonResponse(200, { user_id: "u1", email: "a@finops.local" })],
      [`${API_BASE_PATH}/conversations`]: [jsonResponse(200, { items: [], next_cursor: null })],
      [`${API_BASE_PATH}/realtime/token`]: [jsonResponse(401, PROBLEM_UNAUTHENTICATED)],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })
    await client.bootstrap(depsOf(client))

    const issueTicket = createRealtimeTicketIssuer(client.configuration)
    const error = await issueTicket().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(SessionExpiredError)
    expect(error).not.toBeInstanceOf(FetchError)
  })

  it("issueTicket() отклоняется UnauthenticatedError, когда сессии не было вовсе", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(401, PROBLEM_UNAUTHENTICATED)],
      [`${API_BASE_PATH}/realtime/token`]: [jsonResponse(401, PROBLEM_UNAUTHENTICATED)],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })

    const issueTicket = createRealtimeTicketIssuer(client.configuration)
    const error = await issueTicket().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(UnauthenticatedError)
    expect(error).not.toBeInstanceOf(FetchError)
  })

  it("успешный тикет идёт как есть", async () => {
    const stub = createStubFetch({
      [`${API_BASE_PATH}/auth/refresh`]: [jsonResponse(200, ACCESS_TOKEN)],
      [`${API_BASE_PATH}/realtime/token`]: [jsonResponse(200, { token: "ticket-1" })],
    })
    const client = createApiClient({ fetchImpl: stub.fetchImpl as never })
    await client.refreshAccessToken()

    const issueTicket = createRealtimeTicketIssuer(client.configuration)
    await expect(issueTicket()).resolves.toBe("ticket-1")
  })
})
