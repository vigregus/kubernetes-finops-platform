import { act, cleanup, render } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiProblem } from "../../api/problems"
import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge"
import type { EngineEnv, WireSignal } from "./callEngine"
import type { ApiCallView } from "./callState"
import type { CallsOperations } from "./callsApi"
import { CallsProvider, useCalls, type CallsContextValue } from "./CallsProvider"
import type { AlertEnv } from "./incomingAlert"

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
    iceServers: vi.fn(async () => ({ servers: [], ttlSeconds: 600 })),
    ...overrides,
  }
  return { ops, log }
}

function fakeAlert() {
  const log = { started: 0, stopped: 0, notified: [] as string[] }
  const env: AlertEnv = {
    pageHidden: () => true,
    onVisibilityChange: () => () => undefined,
    getTitle: () => "Messenger",
    setTitle: () => undefined,
    openRingtone: () => {
      log.started += 1
      return { tone: () => undefined, close: () => void (log.stopped += 1) }
    },
    notify: (info) => void log.notified.push(info.callId),
    closeNotification: () => undefined,
    setInterval: () => 0,
    clearInterval: () => undefined,
  }
  return { env, log }
}

function givenProvider(
  ops: CallsOperations,
  alertEnv: AlertEnv = fakeAlert().env,
  engineEnv: EngineEnv = fakeEnv(),
) {
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
      env={engineEnv}
      createCentrifuge={fake.factory}
      alertEnv={alertEnv}
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

describe("качество видео", () => {
  // В окружении тестов своего хранилища нет: подставляется простое, общее на тест.
  const store = new Map<string, string>()
  beforeEach(() => {
    store.clear()
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    })
    Object.defineProperty(window, "localStorage", { value: globalThis.localStorage, configurable: true })
  })
  afterEach(() => vi.unstubAllGlobals())

  it("по умолчанию auto, выбор сохраняется и переживает перемонтирование", async () => {
    const { ops } = fakeOps()
    const first = givenProvider(ops)
    expect(first.ctx.current?.videoQuality).toBe("auto")
    await act(async () => first.ctx.current?.setVideoQuality("low"))
    expect(first.ctx.current?.videoQuality).toBe("low")
    cleanup()

    const second = givenProvider(fakeOps().ops)
    expect(second.ctx.current?.videoQuality).toBe("low")
  })

  it("сохранённое качество действует с первой секунды звонка, а не только после смены", async () => {
    store.set("messenger.call.videoQuality", "low")
    const requests: unknown[] = []
    const env = fakeEnv()
    const original = env.getUserMedia
    env.getUserMedia = async (c) => {
      requests.push(c)
      return original(c)
    }
    const { ops } = fakeOps({ start: vi.fn(async () => api({ role: "caller", kind: "video", state: "accepted", version: 2 })) })
    const { ctx } = givenProvider(ops, undefined, env)
    await act(async () => ctx.current?.startCall(CONV, "video", "Bob"))
    await vi.waitFor(() => expect(requests.length).toBeGreaterThan(0))
    // `low` снимает 720p (а не 1080p, как auto): качество дошло до движка при создании
    expect((requests[0] as { video: { height: { ideal: number } } }).video.height.ideal).toBe(720)
  })

  it("прежнее сохранённое имя `high` читается как HD", () => {
    store.set("messenger.call.videoQuality", "high")
    const { ctx } = givenProvider(fakeOps().ops)
    expect(ctx.current?.videoQuality).toBe("hd")
  })

  it("мусор в хранилище читается как auto", () => {
    store.set("messenger.call.videoQuality", "ultra")
    const { ctx } = givenProvider(fakeOps().ops)
    expect(ctx.current?.videoQuality).toBe("auto")
  })
})

describe("данные TURN", () => {
  it("вызываемый просит их только после ответа сервера на «принять» (иначе call_not_ready)", async () => {
    let release: (call: ApiCallView) => void = () => undefined
    const accept = vi.fn(() => new Promise<ApiCallView>((resolve) => (release = resolve)))
    const { ops } = fakeOps({ accept })
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    // ответа ещё нет: движок поднят, а данные TURN не запрошены
    expect(ops.iceServers).not.toHaveBeenCalled()
    await act(async () => {
      release(api({ state: "accepted", version: 2 }))
      await Promise.resolve()
      await Promise.resolve()
    })
    await vi.waitFor(() => expect(ops.iceServers).toHaveBeenCalledTimes(1))
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


describe("сигнал о входящем звонке", () => {
  it("входящий включает сигнал, любой конец — выключает", async () => {
    const alert = fakeAlert()
    const { ops } = fakeOps()
    const { publish } = givenProvider(ops, alert.env)
    expect(alert.log.started).toBe(0)
    await publish(incoming)
    expect(alert.log.started).toBe(1)
    expect(alert.log.notified).toEqual([CALL])
    await publish({ type: "call.state", call_id: CALL, state: "ended", reason: "cancelled", version: 3 })
    expect(alert.log.stopped).toBe(1)
  })

  it("принятие звонка выключает сигнал", async () => {
    const alert = fakeAlert()
    const { ops } = fakeOps()
    const { ctx, publish } = givenProvider(ops, alert.env)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    expect(alert.log.stopped).toBe(1)
  })
})

describe("действие с уведомления", () => {
  const swMessage = (data: unknown) =>
    act(async () => {
      for (const handler of swHandlers) handler({ data } as MessageEvent)
      await Promise.resolve()
    })

  const swHandlers: Array<(event: MessageEvent) => void> = []
  beforeEach(() => {
    swHandlers.length = 0
    vi.stubGlobal("navigator", {
      ...globalThis.navigator,
      serviceWorker: {
        addEventListener: (_: string, handler: (event: MessageEvent) => void) => swHandlers.push(handler),
        removeEventListener: () => undefined,
      },
    })
  })
  afterEach(() => vi.unstubAllGlobals())

  it("«Принять» с уведомления принимает входящий звонок", async () => {
    const { ops } = fakeOps()
    const { publish } = givenProvider(ops)
    await publish(incoming)
    await swMessage({ type: "call-action", action: "accept", callId: CALL })
    expect(ops.accept).toHaveBeenCalledTimes(1)
  })

  it("«Отклонить» с уведомления отклоняет", async () => {
    const { ops } = fakeOps()
    const { publish } = givenProvider(ops)
    await publish(incoming)
    await swMessage({ type: "call-action", action: "decline", callId: CALL })
    expect(ops.decline).toHaveBeenCalledTimes(1)
    expect(ops.accept).not.toHaveBeenCalled()
  })

  it("действие пришло раньше входящего (приложение открылось из уведомления) — ждёт и применяется", async () => {
    const { ops } = fakeOps()
    const { publish } = givenProvider(ops)
    await swMessage({ type: "call-action", action: "accept", callId: CALL })
    expect(ops.accept).not.toHaveBeenCalled()
    await publish(incoming)
    expect(ops.accept).toHaveBeenCalledTimes(1)
  })

  it("«открыть» ничего не принимает — только показывает экран вызова", async () => {
    const { ops } = fakeOps()
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await swMessage({ type: "call-action", action: "open", callId: CALL })
    expect(ops.accept).not.toHaveBeenCalled()
    expect(ctx.current?.view.phase).toBe("incoming")
  })

  it("действие над чужим звонком не принимает этот", async () => {
    const { ops } = fakeOps()
    const { publish } = givenProvider(ops)
    await publish(incoming)
    await swMessage({ type: "call-action", action: "accept", callId: "другой-звонок" })
    expect(ops.accept).not.toHaveBeenCalled()
  })

  it("мусорные сообщения worker'а игнорируются", async () => {
    const { ops } = fakeOps()
    const { publish } = givenProvider(ops)
    await publish(incoming)
    await swMessage(null)
    await swMessage({ type: "call-action", action: "format-disk", callId: CALL })
    await swMessage({ type: "other" })
    expect(ops.accept).not.toHaveBeenCalled()
  })
})


describe("потерянные события и дубли", () => {
  const offerEvent = (seq: number, id: string) => ({
    type: "call.signal",
    call_id: CALL,
    seq,
    signal_id: id,
    signal: { type: "ice", candidates: [{ candidate: "c" }] },
  })

  it("повтор сигнала с тем же signal_id (потерян ответ, а сигнал дошёл) не применяется второй раз", async () => {
    const { ops } = fakeOps()
    const { ctx, publish } = givenProvider(ops)
    await publish(incoming)
    await act(async () => ctx.current?.accept())
    await publish(offerEvent(1, "sig-A"))
    expect(ctx.current?.view.lastSignalSeq).toBe(1)
    // тот же логический сигнал вторым номером: сервер выдал новый seq, но это дубль
    await publish(offerEvent(2, "sig-A"))
    expect(ctx.current?.view.lastSignalSeq).toBe(1)
    // другой сигнал применяется
    await publish(offerEvent(3, "sig-B"))
    expect(ctx.current?.view.lastSignalSeq).toBe(3)
  })

  it("пока звонок звонит, клиент сам спрашивает сервер: событие «принято» могло потеряться", async () => {
    vi.useFakeTimers()
    try {
      const { ops } = fakeOps({ start: vi.fn(async () => api({ role: "caller" })) })
      const { ctx } = givenProvider(ops)
      await act(async () => ctx.current?.startCall(CONV, "audio", "Bob"))
      expect(ctx.current?.view.phase).toBe("outgoing")
      const before = (ops.current as ReturnType<typeof vi.fn>).mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5100)
      })
      expect((ops.current as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(before)
    } finally {
      vi.useRealTimers()
    }
  })

  it("звонок закончился, а событие потерялось: сверка обнаруживает конец", async () => {
    vi.useFakeTimers()
    try {
      const { ops } = fakeOps({ start: vi.fn(async () => api({ role: "caller" })) })
      const { ctx } = givenProvider(ops)
      await act(async () => ctx.current?.startCall(CONV, "audio", "Bob"))
      // сервер отвечает «живых звонков нет»
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5100)
      })
      expect(ctx.current?.view.phase).toBe("ended")
    } finally {
      vi.useRealTimers()
    }
  })
})
