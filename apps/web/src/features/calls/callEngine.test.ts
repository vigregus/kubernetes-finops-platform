import { describe, expect, it } from "vitest"

import { CallEngine, connectionType, mediaConstraints, type EngineOptions, type WireSignal } from "./callEngine"

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
  stats = new Map<string, Record<string, unknown>>()

  addTrack(track: FakeTrack) {
    this.senders.push({ track })
  }
  getSenders() {
    return this.senders.map((s) => ({
      track: s.track,
      getParameters: () => ({ encodings: [{}] }),
      setParameters: () => Promise.resolve(),
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
    iceServers: async () => [{ urls: "stun:x" }],
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
