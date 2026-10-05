import { describe, expect, it } from "vitest"

import {
  CallEngine,
  VIDEO_MAX_BITRATE,
  VIDEO_PRESETS,
  connectionType,
  encodingFor,
  mediaConstraints,
  type EngineOptions,
  type WireSignal,
} from "./callEngine"

// Двойники: ровно то подмножество WebRTC, которое трогает движок.

class FakeTrack {
  enabled = true
  stopped = false
  readonly kind: "audio" | "video"
  constructor(kind: "audio" | "video") {
    this.kind = kind
  }
  stop() {
    this.stopped = true
  }
}

class FakeStream {
  readonly tracks: FakeTrack[]
  constructor(tracks: FakeTrack[]) {
    this.tracks = tracks
  }
  getTracks() {
    return this.tracks
  }
  getAudioTracks() {
    return this.tracks.filter((t) => t.kind === "audio")
  }
  getVideoTracks() {
    return this.tracks.filter((t) => t.kind === "video")
  }
}

/** Выбранная пара «хост — хост»: прямой путь. */
function directPair() {
  return new Map<string, Record<string, unknown>>([
    ["T", { type: "transport", selectedCandidatePairId: "P" }],
    ["P", { type: "candidate-pair", localCandidateId: "L", remoteCandidateId: "R" }],
    ["L", { type: "local-candidate", candidateType: "host" }],
    ["R", { type: "remote-candidate", candidateType: "host" }],
  ])
}

function relayPair() {
  const stats = directPair()
  stats.set("L", { type: "local-candidate", candidateType: "relay" })
  return stats
}

class FakePeer {
  connectionState: RTCPeerConnectionState = "new"
  localDescription: { type: string; sdp: string } | null = null
  remoteDescription: { type: string; sdp: string } | null = null
  added: RTCIceCandidateInit[] = []
  offers: Array<{ iceRestart?: boolean } | undefined> = []
  closed = false
  ontrack: ((e: unknown) => void) | null = null
  onicecandidate: ((e: { candidate: unknown }) => void) | null = null
  onconnectionstatechange: (() => void) | null = null
  senders: Array<{ track: FakeTrack }> = []
  configuration: RTCConfiguration = {}
  configurations: RTCConfiguration[] = []
  stats = directPair()

  getConfiguration() {
    return this.configuration
  }
  setConfiguration(configuration: RTCConfiguration) {
    this.configuration = configuration
    this.configurations.push(configuration)
  }
  addTrack(track: FakeTrack) {
    this.senders.push({ track })
  }
  applied: Array<{ kind: string; encoding: Record<string, unknown> }> = []
  getSenders() {
    return this.senders.map((s) => ({
      track: s.track,
      getParameters: () => ({ encodings: [{}] }),
      setParameters: (params: { encodings: Array<Record<string, unknown>> }) => {
        this.applied.push({ kind: s.track.kind, encoding: params.encodings[0] ?? {} })
        return Promise.resolve()
      },
    }))
  }
  async createOffer(options?: { iceRestart?: boolean }) {
    this.offers.push(options)
    return { type: "offer" as const, sdp: `offer#${this.offers.length}` }
  }
  async createAnswer() {
    return { type: "answer" as const, sdp: "answer" }
  }
  async setLocalDescription(description: { type: string; sdp: string }) {
    this.localDescription = description
  }
  async setRemoteDescription(description: { type: string; sdp: string }) {
    this.remoteDescription = description
  }
  async addIceCandidate(candidate: RTCIceCandidateInit) {
    if (this.remoteDescription === null) throw new Error("нет удалённого описания")
    this.added.push(candidate)
  }
  getStats() {
    return Promise.resolve(this.stats)
  }
  close() {
    this.closed = true
  }
  emitState(state: RTCPeerConnectionState) {
    this.connectionState = state
    this.onconnectionstatechange?.()
  }
  emitCandidate(text: string) {
    this.onicecandidate?.({ candidate: { toJSON: () => ({ candidate: text }) } })
  }
}

interface Rig {
  engine: CallEngine
  peer: FakePeer
  sent: WireSignal[]
  events: string[]
  timers: Array<{ id: number; fn: () => void; ms: number; cleared: boolean }>
  media: { requests: MediaStreamConstraints[] }
}

function rig(overrides: Partial<EngineOptions> & { mediaFails?: Array<boolean> } = {}): Rig {
  const peer = new FakePeer()
  const sent: WireSignal[] = []
  const events: string[] = []
  const timers: Rig["timers"] = []
  const media: Rig["media"] = { requests: [] }
  const fails = [...(overrides.mediaFails ?? [])]
  const base: EngineOptions = {
    env: {
      getUserMedia: async (constraints) => {
        media.requests.push(constraints)
        if (fails.shift()) throw new Error("denied")
        const tracks = [new FakeTrack("audio")]
        if (constraints.video) tracks.push(new FakeTrack("video"))
        return new FakeStream(tracks) as unknown as MediaStream
      },
      createPeer: () => peer as unknown as RTCPeerConnection,
    },
    role: "caller",
    kind: "audio",
    sendSignal: async (signal) => {
      sent.push(signal)
    },
    iceServers: async () => ({ servers: [{ urls: "stun:x" }], ttlSeconds: 600 }),
    onLocalStream: () => events.push("local"),
    onRemoteStream: () => events.push("remote"),
    onConnected: (type) => events.push(`connected:${type}`),
    onFailed: (reason) => events.push(`failed:${reason}`),
    timers: {
      setTimeout: (fn, ms) => {
        const entry = { id: timers.length + 1, fn, ms, cleared: false }
        timers.push(entry)
        return entry.id
      },
      clearTimeout: (id) => {
        const entry = timers.find((t) => t.id === id)
        if (entry) entry.cleared = true
      },
    },
  }
  const { mediaFails: _ignored, ...rest } = overrides
  void _ignored
  return { engine: new CallEngine({ ...base, ...rest }), peer, sent, events, timers, media }
}

const fire = (r: Rig, ms: number) => {
  for (const t of r.timers.filter((x) => x.ms === ms && !x.cleared)) {
    t.cleared = true
    t.fn()
  }
}

describe("звонящий", () => {
  it("готовит медиа и сразу предлагает offer", async () => {
    const r = rig()
    await r.engine.start()
    expect(r.events).toContain("local")
    expect(r.sent).toEqual([{ type: "offer", sdp: "offer#1" }])
  })

  it("получает answer и применяет кандидатов, пришедших раньше него", async () => {
    const r = rig()
    await r.engine.start()
    await r.engine.handleSignal({ type: "ice", candidates: [{ candidate: "early" }] })
    expect(r.peer.added).toEqual([])
    await r.engine.handleSignal({ type: "answer", sdp: "a" })
    expect(r.peer.added).toEqual([{ candidate: "early" }])
    await r.engine.handleSignal({ type: "ice", candidates: [{ candidate: "late" }] })
    expect(r.peer.added.map((c) => c.candidate)).toEqual(["early", "late"])
  })

  it("собственные кандидаты уходят пачкой, а конец сбора — сразу", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.emitCandidate("c1")
    r.peer.emitCandidate("c2")
    expect(r.sent.filter((s) => s.type === "ice")).toHaveLength(0)
    fire(r, 150)
    const pack = r.sent.filter((s) => s.type === "ice")
    expect(pack).toHaveLength(1)
    expect(pack[0]).toEqual({ type: "ice", candidates: [{ candidate: "c1" }, { candidate: "c2" }] })

    r.peer.emitCandidate("c3")
    r.peer.onicecandidate?.({ candidate: null })
    expect(r.sent.filter((s) => s.type === "ice")).toHaveLength(2)
  })
})

describe("вызываемый", () => {
  it("на offer отвечает answer и не предлагает сам", async () => {
    const r = rig({ role: "callee" })
    await r.engine.start()
    expect(r.sent).toEqual([])
    await r.engine.handleSignal({ type: "offer", sdp: "remote" })
    expect(r.peer.remoteDescription).toEqual({ type: "offer", sdp: "remote" })
    expect(r.sent).toEqual([{ type: "answer", sdp: "answer" }])
  })

  it("сигнал, пришедший до готовности медиа, ждёт своей очереди", async () => {
    const r = rig({ role: "callee" })
    const started = r.engine.start()
    const handled = r.engine.handleSignal({ type: "offer", sdp: "early" })
    await Promise.all([started, handled])
    expect(r.sent).toEqual([{ type: "answer", sdp: "answer" }])
  })
})

describe("медиа", () => {
  it("видеозвонок просит камеру и микрофон с потолком 720p", async () => {
    const r = rig({ kind: "video" })
    await r.engine.start()
    expect(r.media.requests[0]?.video).toMatchObject({ height: { max: 720 } })
    expect(r.engine.hasVideo()).toBe(true)
  })

  it("камера закрыта — звонок состоится как аудио", async () => {
    const r = rig({ kind: "video", mediaFails: [true, false] })
    await r.engine.start()
    expect(r.engine.hasVideo()).toBe(false)
    expect(r.events).not.toContain("failed:media_denied")
    expect(r.sent[0]?.type).toBe("offer")
  })

  it("микрофон закрыт — звонок не состоится, причина названа", async () => {
    const r = rig({ mediaFails: [true] })
    await r.engine.start()
    expect(r.events).toEqual(["failed:media_denied"])
    expect(r.sent).toEqual([])
  })

  it("микрофон и камера выключаются без разрыва соединения", async () => {
    const r = rig({ kind: "video" })
    await r.engine.start()
    r.engine.setMuted(true)
    r.engine.setCameraOff(true)
    const tracks = r.peer.senders.map((s) => s.track)
    expect(tracks.find((t) => t.kind === "audio")?.enabled).toBe(false)
    expect(tracks.find((t) => t.kind === "video")?.enabled).toBe(false)
    expect(r.peer.closed).toBe(false)
  })

  it("ограничение аудио-звонка: видео не запрашивается", () => {
    expect(mediaConstraints("audio").video).toBe(false)
  })
})

describe("соединение и обрыв (CALL-011)", () => {
  it("медиа пошло — путь соединения сообщается один раз", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.stats = new Map([
      ["T", { type: "transport", selectedCandidatePairId: "P" }],
      ["P", { type: "candidate-pair", localCandidateId: "L", remoteCandidateId: "R" }],
      ["L", { type: "local-candidate", candidateType: "host" }],
      ["R", { type: "remote-candidate", candidateType: "srflx" }],
    ])
    r.peer.emitState("connected")
    r.peer.emitState("connected")
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual(["connected:direct"])
  })

  it("через релей — путь relay", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.stats = new Map([
      ["T", { type: "transport", selectedCandidatePairId: "P" }],
      ["P", { type: "candidate-pair", localCandidateId: "L", remoteCandidateId: "R" }],
      ["L", { type: "local-candidate", candidateType: "relay" }],
      ["R", { type: "remote-candidate", candidateType: "host" }],
    ])
    expect(await connectionType(r.peer as unknown as RTCPeerConnection)).toBe("relay")
  })

  it("обрыв 10 с переживается: окно сброшено возвращением связи", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.emitState("disconnected")
    expect(r.timers.some((t) => t.ms === 15_000 && !t.cleared)).toBe(true)
    r.peer.emitState("connected")
    fire(r, 15_000)
    expect(r.events).not.toContain("failed:failed")
  })

  it("обрыв 20 с — звонок завершается неудачей", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.emitState("disconnected")
    fire(r, 15_000)
    expect(r.events).toContain("failed:failed")
    expect(r.peer.closed).toBe(true)
  })

  it("звонящий перезапускает ICE новым offer, вызываемый — нет", async () => {
    const caller = rig()
    await caller.engine.start()
    caller.peer.emitState("failed")
    await caller.engine.handleSignal({ type: "answer", sdp: "x" }).catch(() => undefined)
    expect(caller.peer.offers.some((o) => o?.iceRestart === true)).toBe(true)

    const callee = rig({ role: "callee" })
    await callee.engine.start()
    callee.peer.emitState("failed")
    expect(callee.peer.offers).toEqual([])
  })

  it("после закрытия сигналы и события игнорируются", async () => {
    const r = rig()
    await r.engine.start()
    r.engine.close()
    await r.engine.handleSignal({ type: "answer", sdp: "x" })
    r.peer.emitState("connected")
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual([])
    expect(r.peer.closed).toBe(true)
  })

  it("сбой применения сигнала завершает звонок, а не виснет", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.setRemoteDescription = async () => {
      throw new Error("битый SDP")
    }
    await r.engine.handleSignal({ type: "answer", sdp: "x" })
    expect(r.events).toContain("failed:failed")
  })
})


describe("обновление данных TURN (долгий релейный звонок)", () => {
  /** Движок, у которого `iceServers` выдаёт пронумерованные данные и срок. */
  function refreshRig(ttlSeconds = 600) {
    const issued: string[] = []
    const r = rig({
      iceServers: async () => {
        issued.push(`n${issued.length + 1}`)
        return {
          servers: [{ urls: "turn:x", username: `u${issued.length}`, credential: "c" }],
          ttlSeconds,
        }
      },
    })
    return { ...r, issued }
  }

  it("новые данные берутся за минуту до конца срока и ставятся в соединение", async () => {
    const r = refreshRig(600)
    await r.engine.start()
    expect(r.issued).toHaveLength(1)
    // таймер обновления: срок минус минута
    expect(r.timers.some((t) => t.ms === 540_000 && !t.cleared)).toBe(true)

    fire(r, 540_000)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(r.issued).toHaveLength(2)
    const applied = r.peer.configurations.at(-1)?.iceServers?.[0] as { username: string } | undefined
    expect(applied?.username).toBe("u2")
    // и следующее обновление уже назначено
    expect(r.timers.some((t) => t.ms === 540_000 && !t.cleared)).toBe(true)
  })

  it("короткий срок не даёт частить: не чаще чем раз в 30 секунд", async () => {
    const r = refreshRig(70)
    await r.engine.start()
    expect(r.timers.some((t) => t.ms === 30_000)).toBe(true)
  })

  it("неудачное обновление повторяется через 20 секунд, звонок не рвётся", async () => {
    let calls = 0
    const r = rig({
      iceServers: async () => {
        calls += 1
        if (calls === 2) throw new Error("API недоступно")
        return { servers: [{ urls: "turn:x" }], ttlSeconds: 600 }
      },
    })
    await r.engine.start()
    fire(r, 540_000)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(r.events).not.toContain("failed:failed")
    expect(r.timers.some((t) => t.ms === 20_000 && !t.cleared)).toBe(true)
    fire(r, 20_000)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(calls).toBe(3)
  })

  it("после закрытия обновление не идёт", async () => {
    const r = refreshRig(600)
    await r.engine.start()
    r.engine.close()
    expect(r.timers.filter((t) => t.ms === 540_000).every((t) => t.cleared)).toBe(true)
    fire(r, 540_000)
    expect(r.issued).toHaveLength(1)
  })
})


describe("качество исходящего видео", () => {
  it("auto — потолок оценки стоимости и без масштабирования", () => {
    expect(encodingFor("auto")).toEqual({
      maxBitrate: VIDEO_MAX_BITRATE,
      scaleResolutionDownBy: 1,
      maxFramerate: 30,
    })
  })

  it("пресеты строго по возрастанию: ниже — меньше кадр, битрейт и частота", () => {
    const { low, medium, high } = VIDEO_PRESETS
    expect(low.maxBitrate).toBeLessThan(medium.maxBitrate)
    expect(medium.maxBitrate).toBeLessThan(high.maxBitrate)
    expect(low.scaleResolutionDownBy).toBeGreaterThan(medium.scaleResolutionDownBy)
    expect(medium.scaleResolutionDownBy).toBeGreaterThan(high.scaleResolutionDownBy)
    expect(low.maxFramerate).toBeLessThan(high.maxFramerate)
  })

  it("при установлении связи применяется начальное качество, и только к видео", async () => {
    const r = rig({ kind: "video", videoQuality: "low" })
    await r.engine.start()
    r.peer.emitState("connected")
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(r.peer.applied).toEqual([{ kind: "video", encoding: expect.objectContaining(VIDEO_PRESETS.low) }])
  })

  it("смена качества посреди звонка не пересоздаёт соединение", async () => {
    const r = rig({ kind: "video" })
    await r.engine.start()
    r.peer.emitState("connected")
    await new Promise((resolve) => setTimeout(resolve, 0))
    r.peer.applied.length = 0

    r.engine.setVideoQuality("high")
    r.engine.setVideoQuality("medium")
    expect(r.peer.applied.map((a) => a.encoding["maxBitrate"])).toEqual([
      VIDEO_PRESETS.high.maxBitrate,
      VIDEO_PRESETS.medium.maxBitrate,
    ])
    expect(r.peer.closed).toBe(false)
  })

  it("возврат на auto снимает масштаб, а не оставляет прежний пресет", async () => {
    const r = rig({ kind: "video", videoQuality: "low" })
    await r.engine.start()
    r.engine.setVideoQuality("auto")
    expect(r.peer.applied.at(-1)?.encoding).toMatchObject({ scaleResolutionDownBy: 1, maxBitrate: VIDEO_MAX_BITRATE })
  })

  it("аудиозвонок: качество видео ничего не трогает", async () => {
    const r = rig({ kind: "audio" })
    await r.engine.start()
    r.engine.setVideoQuality("low")
    expect(r.peer.applied).toEqual([])
  })
})


describe("путь соединения при смене сети (метрика стоимости)", () => {
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

  it("сменился прямой путь на релейный после перезапуска ICE — сообщается и релей", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.emitState("connected")
    await settle()
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual(["connected:direct"])

    // сеть сменилась: связь потеряна и вернулась уже через TURN
    r.peer.emitState("disconnected")
    r.peer.stats = relayPair()
    r.peer.emitState("connected")
    await settle()
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual([
      "connected:direct",
      "connected:relay",
    ])
  })

  it("повторное «connected» с тем же путём не шлёт сообщений", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.emitState("connected")
    await settle()
    r.peer.emitState("disconnected")
    r.peer.emitState("connected")
    await settle()
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual(["connected:direct"])
  })

  it("пара ещё не выбрана — измерение повторяется, а не фиксирует direct сразу", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.stats = new Map()
    r.peer.emitState("connected")
    await settle()
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual([])
    // пара появилась до следующей попытки
    r.peer.stats = relayPair()
    fire(r, 500)
    await settle()
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual(["connected:relay"])
  })

  it("пара так и не определилась — первое сообщение уходит как direct, метрика не молчит", async () => {
    const r = rig()
    await r.engine.start()
    r.peer.stats = new Map()
    r.peer.emitState("connected")
    for (let i = 0; i < 4; i += 1) {
      await settle()
      fire(r, 500)
    }
    await settle()
    expect(r.events.filter((e) => e.startsWith("connected"))).toEqual(["connected:direct"])
  })
})
