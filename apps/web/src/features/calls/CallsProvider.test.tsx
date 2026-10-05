import { act, cleanup, render } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiProblem } from "../../api/problems"
import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge"
import type { EngineEnv, WireSignal } from "./callEngine"
import type { ApiCallView } from "./callState"
import type { CallsOperations } from "./callsApi"
import { CallsProvider, useCalls, type CallsContextValue } from "./CallsProvider"

afterEach(cleanup)

const VIEWER = "11111111-1111-1111-1111-111111111111"
const CALL = "44444444-4444-4444-4444-444444444444"
const CONV = "33333333-3333-3333-3333-333333333333"

function api(overrides: Partial<ApiCallView> = {}): ApiCallView {
  return {
    callId: CALL,
    conversationId: CONV,
    kind: "audio",
    state: "ringing",
    endReason: null,
    version: 1,
    role: "callee",
    ...overrides,
  }
}

/** Двойник WebRTC достаточный, чтобы движок дошёл до «ждёт offer». */
function fakeEnv(): EngineEnv {
  const peer = {
    connectionState: "new",
    addTrack: () => undefined,
    close: () => undefined,
    ontrack: null,
    onicecandidate: null,
    onconnectionstatechange: null,
    getSenders: () => [],
    getStats: async () => new Map(),
  }
  return {
    getUserMedia: async () =>
      ({ getTracks: () => [], getAudioTracks: () => [], getVideoTracks: () => [] }) as unknown as MediaStream,
    createPeer: () => peer as unknown as RTCPeerConnection,
  }
}

function fakeOps(overrides: Partial<CallsOperations> = {}) {
  const log: string[] = []
  const ops: CallsOperations = {
    start: vi.fn(async () => api({ role: "caller" })),
    current: vi.fn(async () => null),
    accept: vi.fn(async () => api({ state: "accepted", version: 2 })),
    decline: vi.fn(async () => api({ state: "ended", endReason: "declined", version: 2 })),
    hangup: vi.fn(async (...args: unknown[]) => {
      log.push(`hangup:${JSON.stringify(args.slice(1))}`)
      return api({ state: "ended", endReason: "completed", version: 9 })
    }),
    fail: vi.fn(async () => api({ state: "ended", endReason: "failed", version: 9 })),
    keepalive: vi.fn(async () => api()),
    connected: vi.fn(async () => api()),
    signal: vi.fn(async (_id: string, _signal: WireSignal) => undefined),
    iceServers: vi.fn(async () => []),
    ...overrides,
  }
  return { ops, log }
}

function givenProvider(ops: CallsOperations) {
  const fake = givenFakeCentrifuge()
  const ctx: { current: CallsContextValue | null } = { current: null }
  function Probe() {
    ctx.current = useCalls()
    return null
  }
  render(
    <CallsProvider
      ops={ops}
      centrifugoUrl="wss://rt.test/connection/websocket"
      issueCallsTicket={givenTicketIssuer().issueTicket}
      viewerId={VIEWER}
      peerNameFor={() => "Alice"}
      env={fakeEnv()}
      createCentrifuge={fake.factory}
    >
      <Probe />
    </CallsProvider>,
  )
  const publish = (data: unknown) =>
    act(async () => {
      fake.clientHandlers["publication"]?.({ channel: `call:${VIEWER}`, data })
      await Promise.resolve()
    })
  const connect = () =>
    act(async () => {
      fake.clientHandlers["connected"]?.({})
      await Promise.resolve()
      await Promise.resolve()
    })
  return { fake, ctx, publish, connect }
}

const incoming = {
  type: "call.incoming",
  call_id: CALL,
  conversation_id: CONV,
  kind: "audio",
  caller: { user_id: "x", display_name: "Alice" },
  version: 1,
}

describe("сверка при подъёме сигнализации", () => {
  it("вкладка, открытая посреди чужого идущего звонка, его НЕ завершает", async () => {
    const { ops } = fakeOps({ current: vi.fn(async () => api({ state: "active", role: "caller", version: 5 })) })
    const { ctx, connect } = givenProvider(ops)
    await connect()
    expect(ops.hangup).not.toHaveBeenCalled()
    expect(ops.fail).not.toHaveBeenCalled()
    expect(ctx.current?.view.phase).toBe("idle")
  })

  it("пока звонит входящий, свободная вкладка его показывает", async () => {
    const { ops } = fakeOps({ current: vi.fn(async () => api()) })
    const { ctx, connect } = givenProvider(ops)
    await connect()
    expect(ctx.current?.view).toMatchObject({ phase: "incoming", peerName: "Alice" })
  })

  it("событие, потерянное при обрыве, возвращается сверкой после переподключения", async () => {
    const calls = vi.fn(async () => null as ApiCallView | null)
    const { ops } = fakeOps({ current: calls })
    const { ctx, connect } = givenProvider(ops)
    await connect()
    expect(ctx.current?.view.phase).toBe("idle")

    // пока соединение звонков было мертво, пришёл входящий — его никто не видел
    calls.mockResolvedValue(api())
    await connect()
    expect(ctx.current?.view.phase).toBe("incoming")
  })

  it("у нас идёт звонок, а сервер его не знает, — конец потерян: звонок завершается локально", async () => {
    const calls = vi.fn(async () => null as ApiCallView | null)
    const { ops } = fakeOps({ current: calls })
    const { ctx, publish, connect } = givenProvider(ops)
    await publish(incoming)
    expect(ctx.current?.view.phase).toBe("incoming")
    await connect()
    expect(ctx.current?.view).toMatchObject({ phase: "ended", endReason: "failed" })
    // конец уже случился на сервере: повторно завершать нечего
    expect(ops.hangup).not.toHaveBeenCalled()
    expect(ops.fail).not.toHaveBeenCalled()
  })
})

describe("принять в двух вкладках", () => {
  it("«принять» уходит с меткой вкладки", async () => {
    const { ops } = fakeOps()
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    const [, tab] = (ops.accept as ReturnType<typeof vi.fn>).mock.calls[0] as [string, string]
    expect(tab).toMatch(/[0-9a-f-]{36}/)
  })

  it("проигравшая вкладка сворачивается и НЕ вешает трубку победительницы", async () => {
    const taken = new ApiProblem({ status: 409, code: "call_taken", title: "x" } as never)
    const { ops } = fakeOps({ accept: vi.fn(async () => Promise.reject(taken)) })
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    expect(ctx.current?.view).toMatchObject({ phase: "ended", endReason: "accepted_elsewhere" })
    expect(ops.hangup).not.toHaveBeenCalled()
    expect(ops.fail).not.toHaveBeenCalled()
  })

  it("прочая ошибка принятия — звонок не состоялся", async () => {
    const { ops } = fakeOps({ accept: vi.fn(async () => Promise.reject(new Error("сеть"))) })
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    expect(ctx.current?.view).toMatchObject({ phase: "ended", endReason: "failed" })
  })
})

describe("конец звонка", () => {
  it("обычная трубка — hangup, не fail", async () => {
    const { ops } = fakeOps()
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    await act(async () => ctx.current?.hangup())
    expect(ops.hangup).toHaveBeenCalledTimes(1)
    expect(ops.fail).not.toHaveBeenCalled()
  })

  it("страница закрывается посреди звонка — трубка с keepalive, чтобы запрос пережил выгрузку", async () => {
    const { ops, log } = fakeOps()
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"))
    })
    expect(log).toEqual([`hangup:[{"unload":true}]`])
  })

  it("страница без звонка при закрытии ничего не шлёт", async () => {
    const { ops } = fakeOps()
    givenProvider(ops)
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"))
    })
    expect(ops.hangup).not.toHaveBeenCalled()
  })

  it("устаревшее событие того же звонка не воскрешает его", async () => {
    const { ops } = fakeOps()
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await publish({ type: "call.state", call_id: CALL, state: "ended", reason: "cancelled", version: 3 })
    expect(ctx.current?.view.phase).toBe("ended")
    await publish({ type: "call.state", call_id: CALL, state: "accepted", reason: null, version: 2 })
    expect(ctx.current?.view.phase).toBe("ended")
  })
})
