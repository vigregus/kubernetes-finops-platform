import { describe, expect, it } from "vitest"

import {
  CallDiagnostics,
  runProbes,
  type DiagnosticReport,
  type DiagnosticsOps,
  type ProbeEnv,
} from "./networkDiagnostics"

type Behavior = "relay" | "srflx" | "end" | "silent" | "throw"

class FakePeer {
  onicecandidate: ((e: { candidate: { type: string } | null }) => void) | null = null
  closed = false
  private readonly behavior: Behavior
  constructor(behavior: Behavior) {
    this.behavior = behavior
  }
  createDataChannel() {
    return {}
  }
  async createOffer() {
    if (this.behavior === "throw") throw new Error("boom")
    return { type: "offer" as const, sdp: "x" }
  }
  async setLocalDescription() {
    if (this.behavior === "relay") this.onicecandidate?.({ candidate: { type: "relay" } })
    if (this.behavior === "srflx") this.onicecandidate?.({ candidate: { type: "srflx" } })
    if (this.behavior === "end") this.onicecandidate?.({ candidate: null })
  }
  close() {
    this.closed = true
  }
}

const URLS = {
  stun: "stun:s:3478",
  udp: "turn:t:3478?transport=udp",
  tcp: "turn:t:3478?transport=tcp",
  tls: "turns:t:443?transport=tcp",
}
const SERVERS: RTCIceServer[] = [
  { urls: [URLS.stun] },
  { urls: [URLS.udp, URLS.tcp, URLS.tls], username: "u", credential: "c" },
]

function env(
  decide: (config: RTCConfiguration) => Behavior,
  extra: Partial<ProbeEnv> = {},
) {
  const peers: FakePeer[] = []
  const timers: Array<{ fn: () => void; ms: number; cleared: boolean }> = []
  const probeEnv: ProbeEnv = {
    createPeer: (config) => {
      const peer = new FakePeer(decide(config))
      peers.push(peer)
      return peer as unknown as RTCPeerConnection
    },
    https: async () => true,
    websocketUp: () => true,
    setTimeout: (fn, ms) => {
      const entry = { fn, ms, cleared: false }
      timers.push(entry)
      return entry
    },
    clearTimeout: (id) => {
      ;(id as { cleared: boolean }).cleared = true
    },
    ...extra,
  }
  return { probeEnv, peers, timers }
}

/** Какой транспорт проверяет это соединение — по адресам, которые ему дали. */
function transportOf(config: RTCConfiguration): "stun" | "udp" | "tcp" | "tls" {
  const urls = (config.iceServers ?? []).flatMap((s) => (Array.isArray(s.urls) ? s.urls : [s.urls]))
  if (urls.some((u) => u.startsWith("turns:"))) return "tls"
  if (urls.some((u) => u.includes("transport=tcp"))) return "tcp"
  if (urls.some((u) => u.startsWith("turn:"))) return "udp"
  return "stun"
}

const results = (list: Awaited<ReturnType<typeof runProbes>>) =>
  Object.fromEntries(list.map((p) => [p.probe, p.result]))

describe("пробы", () => {
  it("все пути работают — все пробы ok, sse пропущена", async () => {
    const e = env((c) => (transportOf(c) === "stun" ? "srflx" : "relay"))
    expect(results(await runProbes(e.probeEnv, SERVERS))).toEqual({
      https: "ok",
      websocket: "ok",
      sse: "skipped",
      stun: "ok",
      turn_udp: "ok",
      turn_tcp: "ok",
      turn_tls: "ok",
    })
    expect(e.peers.every((p) => p.closed)).toBe(true)
  })

  it("UDP и TCP режутся, TLS жив: видно, что спасёт звонок", async () => {
    const e = env((c) => (transportOf(c) === "tls" ? "relay" : "silent"))
    const pending = runProbes(e.probeEnv, SERVERS)
    await new Promise((resolve) => setTimeout(resolve, 0))
    for (const t of e.timers.filter((x) => !x.cleared)) t.fn()
    const r = results(await pending)
    expect(r).toMatchObject({ stun: "timeout", turn_udp: "timeout", turn_tcp: "timeout", turn_tls: "ok" })
  })

  it("конец сбора без relay — отказ, а не ожидание срока", async () => {
    const e = env((c) => (transportOf(c) === "stun" ? "srflx" : "end"))
    expect(results(await runProbes(e.probeEnv, SERVERS))).toMatchObject({
      turn_udp: "failed",
      turn_tcp: "failed",
      turn_tls: "failed",
    })
  })

  it("нет адресов транспорта — skipped, соединение не создаётся", async () => {
    const e = env(() => "relay")
    const onlyTls: RTCIceServer[] = [{ urls: [URLS.tls], username: "u", credential: "c" }]
    expect(results(await runProbes(e.probeEnv, onlyTls))).toMatchObject({
      stun: "skipped",
      turn_udp: "skipped",
      turn_tcp: "skipped",
      turn_tls: "ok",
    })
    expect(e.peers).toHaveLength(1)
  })

  it("исключение при создании соединения — failed, а не падение диагностики", async () => {
    const e = env(() => "throw")
    const r = results(await runProbes(e.probeEnv, SERVERS))
    expect(r["turn_tls"]).toBe("failed")
  })

  it("https не прошёл и websocket неизвестен", async () => {
    const e = env((c) => (transportOf(c) === "stun" ? "srflx" : "relay"), {
      https: async () => Promise.reject(new Error("net")),
      websocketUp: () => null,
    })
    expect(results(await runProbes(e.probeEnv, SERVERS))).toMatchObject({
      https: "failed",
      websocket: "skipped",
    })
  })

  it("websocket: поднят — ok, нет — failed", async () => {
    const down = env((c) => (transportOf(c) === "stun" ? "srflx" : "relay"), { websocketUp: () => false })
    expect(results(await runProbes(down.probeEnv, SERVERS))["websocket"]).toBe("failed")
  })

  it("STUN без кандидата типа srflx не засчитывается", async () => {
    const e = env((c) => (transportOf(c) === "stun" ? "relay" : "relay"))
    // кандидат relay на STUN-пробе не равен srflx: ждём срока
    const pending = runProbes(e.probeEnv, SERVERS)
    await new Promise((resolve) => setTimeout(resolve, 0))
    for (const t of e.timers.filter((x) => !x.cleared)) t.fn()
    expect(results(await pending)["stun"]).toBe("timeout")
  })
})

function ops() {
  const reports: Array<{ id: string; report: DiagnosticReport }> = []
  let sessions = 0
  const value: DiagnosticsOps & { fail?: "session" | "report" } = {
    session: async () => {
      sessions += 1
      if (value.fail === "session") throw new Error("429")
      return { diagnosticId: "diag-1", ttlSeconds: 45, servers: SERVERS }
    },
    report: async (id, report) => {
      if (value.fail === "report") throw new Error("500")
      reports.push({ id, report })
    },
  }
  return { value, reports, sessions: () => sessions }
}

describe("CallDiagnostics: «до» и «после»", () => {
  const relayAll = (c: RTCConfiguration): Behavior => (transportOf(c) === "stun" ? "srflx" : "relay")

  it("до: пробы и отчёт failure; после: что спасло звонок", async () => {
    const o = ops()
    const d = new CallDiagnostics(o.value, env(relayAll).probeEnv)
    await d.begin("fallback_enabled", "timeout")
    await d.recovered("fallback_enabled", {
      mediaPath: "relay",
      turnTransport: "tls",
      profile: "restricted",
      setupMs: 11000,
      gatheringMs: 300,
    })
    expect(o.reports.map((r) => r.id)).toEqual(["diag-1", "diag-1"])
    expect(o.reports[0].report).toMatchObject({
      phase: "failure",
      trigger: "fallback_enabled",
      setupResult: "timeout",
    })
    expect(o.reports[0].report.probes).toHaveLength(7)
    expect(o.reports[1].report).toEqual({
      phase: "recovery",
      trigger: "fallback_enabled",
      setupResult: "connected",
      selectedCandidate: "relay",
      selectedRelayProtocol: "tls",
      iceConnectionMs: 11000,
      iceGatheringMs: 300,
    })
  })

  it("одна диагностика на звонок: повторное begin пустое", async () => {
    const o = ops()
    const d = new CallDiagnostics(o.value, env(relayAll).probeEnv)
    await d.begin("ice_failed")
    await d.begin("setup_timeout")
    expect(o.sessions()).toBe(1)
  })

  it("причина отказа уходит в отчёт", async () => {
    const o = ops()
    const d = new CallDiagnostics(o.value, env(relayAll).probeEnv)
    await d.begin("setup_timeout", "failed", "ice_connect_timeout")
    expect(o.reports[0].report.failureReason).toBe("ice_connect_timeout")
  })

  it("быстрый fallback: «после» пришло раньше сессии — ждёт идентификатор и уходит с тем же", async () => {
    const reports: Array<{ id: string; report: DiagnosticReport }> = []
    let release: (value: { diagnosticId: string; ttlSeconds: number; servers: RTCIceServer[] }) => void = () => {}
    const slow: DiagnosticsOps = {
      session: () => new Promise((resolve) => (release = resolve)),
      report: async (id, report) => {
        reports.push({ id, report })
      },
    }
    const d = new CallDiagnostics(slow, env(relayAll).probeEnv)
    const begun = d.begin("fallback_enabled", "timeout")
    await d.recovered("fallback_enabled", { mediaPath: "relay", turnTransport: "tls" })
    expect(reports).toHaveLength(0)
    release({ diagnosticId: "diag-7", ttlSeconds: 45, servers: SERVERS })
    await begun
    expect(reports.map((r) => [r.id, r.report.phase])).toEqual([
      ["diag-7", "recovery"],
      ["diag-7", "failure"],
    ])
    expect(reports[0].report).toMatchObject({ selectedCandidate: "relay", selectedRelayProtocol: "tls" })
  })

  it("сессия не открылась — отложенное «после» тихо отбрасывается", async () => {
    const o = ops()
    o.value.fail = "session"
    const d = new CallDiagnostics(o.value, env(relayAll).probeEnv)
    const begun = d.begin("fallback_enabled")
    await d.recovered("fallback_enabled", { mediaPath: "relay" })
    await begun
    expect(o.reports).toHaveLength(0)
  })

  it("«после» без «до» не отправляется", async () => {
    const o = ops()
    const d = new CallDiagnostics(o.value, env(relayAll).probeEnv)
    await d.recovered("fallback_enabled", { mediaPath: "relay" })
    expect(o.reports).toHaveLength(0)
  })

  it("«после» отправляется один раз", async () => {
    const o = ops()
    const d = new CallDiagnostics(o.value, env(relayAll).probeEnv)
    await d.begin("fallback_enabled")
    await d.recovered("fallback_enabled", { mediaPath: "relay" })
    await d.recovered("fallback_enabled", { mediaPath: "relay" })
    expect(o.reports.filter((r) => r.report.phase === "recovery")).toHaveLength(1)
  })

  it("отказ сессии (лимит 429) и отчёта — тишина, исключений наружу нет", async () => {
    const a = ops()
    a.value.fail = "session"
    await expect(new CallDiagnostics(a.value, env(relayAll).probeEnv).begin("ice_failed")).resolves.toBeUndefined()
    const b = ops()
    b.value.fail = "report"
    const d = new CallDiagnostics(b.value, env(relayAll).probeEnv)
    await expect(d.begin("ice_failed")).resolves.toBeUndefined()
    await expect(d.recovered("ice_failed", {})).resolves.toBeUndefined()
  })
})
