/**
 * WebRTC звонка: медиа, соединение, обмен сигналами. Без React и без сети API —
 * всё внешнее приходит параметрами (`EngineEnv`, `sendSignal`), и поэтому каждый
 * исход предъявляется прогоном без браузера.
 *
 * Роли простые и несимметричные, и это намеренно: **звонящий предлагает**
 * (`offer`) после того, как вызываемый принял, **вызываемый отвечает**
 * (`answer`). «Идеальное согласование» (оба могут предлагать) сложнее и нужно
 * тому, кто меняет состав треков посреди звонка; здесь состав фиксирован.
 *
 * Сигналы обрабатываются **строго по очереди** (`chain`): `offer` не должен
 * обгонять подготовку медиа, а кандидаты, пришедшие раньше `answer`, ждут его
 * (иначе `addIceCandidate` падает на пустом удалённом описании).
 *
 * Обрыв: звонок не умирает сразу. `disconnected` и `failed` запускают окно
 * `reconnectWindowMs` (15 с), в течение которого **звонящий** перезапускает ICE
 * (новый `offer` с `iceRestart`); не вернулись — звонок завершается как
 * `failed` (`CALL-007`). Перезапускает один и тот же участник, чтобы два
 * встречных `offer` не столкнулись.
 */

export type WireSignal =
  | { readonly type: "offer" | "answer"; readonly sdp: string }
  | { readonly type: "ice"; readonly candidates: readonly RTCIceCandidateInit[] }

export type ConnectionType = "direct" | "relay"

export interface EngineEnv {
  getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream>
  createPeer(config: RTCConfiguration): RTCPeerConnection
}

export interface EngineOptions {
  readonly env: EngineEnv
  readonly role: "caller" | "callee"
  readonly kind: "audio" | "video"
  readonly sendSignal: (signal: WireSignal) => Promise<void>
  readonly iceServers: () => Promise<RTCIceServer[]>
  readonly onLocalStream: (stream: MediaStream) => void
  readonly onRemoteStream: (stream: MediaStream) => void
  /** Медиа пошло; путь соединения — по `getStats()`. Один раз за звонок. */
  readonly onConnected: (type: ConnectionType) => void
  /** Звонок не состоялся или оборвался: клиент завершает его. */
  readonly onFailed: (reason: "media_denied" | "failed") => void
  readonly reconnectWindowMs?: number
  readonly candidateFlushMs?: number
  /** Подмена таймеров — для прогона без реального времени. */
  readonly timers?: {
    readonly setTimeout: (fn: () => void, ms: number) => unknown
    readonly clearTimeout: (id: unknown) => void
  }
}

/** Потолок видео — 720p; в оценке стоимости заложен 1 Мбит/с на участника. */
export const VIDEO_MAX_BITRATE = 1_000_000

export function mediaConstraints(kind: "audio" | "video"): MediaStreamConstraints {
  return {
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    video:
      kind === "video"
        ? {
            facingMode: "user",
            width: { ideal: 1280, max: 1280 },
            height: { ideal: 720, max: 720 },
            frameRate: { ideal: 30, max: 30 },
          }
        : false,
  }
}

export class CallEngine {
  private readonly opts: EngineOptions
  private pc: RTCPeerConnection | null = null
  private local: MediaStream | null = null
  private haveRemote = false
  private pendingRemote: RTCIceCandidateInit[] = []
  private outgoing: RTCIceCandidateInit[] = []
  private flushTimer: unknown = null
  private reconnectTimer: unknown = null
  private chain: Promise<void> = Promise.resolve()
  private closed = false
  private reported = false

  constructor(options: EngineOptions) {
    this.opts = options
  }

  private get timers() {
    return (
      this.opts.timers ?? {
        setTimeout: (fn: () => void, ms: number) => globalThis.setTimeout(fn, ms),
        clearTimeout: (id: unknown) => globalThis.clearTimeout(id as number),
      }
    )
  }

  private enqueue(task: () => Promise<void>): Promise<void> {
    const next = this.chain.then(async () => {
      if (this.closed) return
      try {
        await task()
      } catch {
        this.fail("failed")
      }
    })
    this.chain = next
    return next
  }

  private fail(reason: "media_denied" | "failed"): void {
    if (this.closed) return
    this.close()
    this.opts.onFailed(reason)
  }

  /** Готовит медиа и соединение; звонящий сразу предлагает. */
  start(): Promise<void> {
    return this.enqueue(async () => {
      const stream = await this.acquireMedia()
      if (stream === null) {
        this.fail("media_denied")
        return
      }
      if (this.closed) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      this.local = stream
      this.opts.onLocalStream(stream)

      const servers = await this.opts.iceServers()
      const pc = this.opts.env.createPeer({ iceServers: servers })
      this.pc = pc
      stream.getTracks().forEach((track) => pc.addTrack(track, stream))

      pc.ontrack = (event) => {
        const remote = event.streams[0] ?? new MediaStream([event.track])
        this.opts.onRemoteStream(remote)
      }
      pc.onicecandidate = (event) => this.collect(event.candidate)
      pc.onconnectionstatechange = () => this.onStateChange()

      if (this.opts.role === "caller") await this.offer(false)
    })
  }

  /** Камера может быть закрыта, а звонок — состояться как аудио. */
  private async acquireMedia(): Promise<MediaStream | null> {
    const { env, kind } = this.opts
    try {
      return await env.getUserMedia(mediaConstraints(kind))
    } catch {
      if (kind === "video") {
        try {
          return await env.getUserMedia(mediaConstraints("audio"))
        } catch {
          return null
        }
      }
      return null
    }
  }

  handleSignal(signal: WireSignal): Promise<void> {
    return this.enqueue(async () => {
      const pc = this.pc
      if (pc === null) return
      if (signal.type === "ice") {
        for (const candidate of signal.candidates) {
          if (this.haveRemote) await pc.addIceCandidate(candidate)
          else this.pendingRemote.push(candidate)
        }
        return
      }
      await pc.setRemoteDescription({ type: signal.type, sdp: signal.sdp })
      this.haveRemote = true
      for (const candidate of this.pendingRemote.splice(0)) await pc.addIceCandidate(candidate)
      if (signal.type === "offer") {
        const answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await this.opts.sendSignal({ type: "answer", sdp: answer.sdp ?? "" })
      }
    })
  }

  private async offer(iceRestart: boolean): Promise<void> {
    const pc = this.pc
    if (pc === null) return
    const description = await pc.createOffer(iceRestart ? { iceRestart: true } : undefined)
    await pc.setLocalDescription(description)
    await this.opts.sendSignal({ type: "offer", sdp: description.sdp ?? "" })
  }

  // --- кандидаты пачками ------------------------------------------------------

  private collect(candidate: RTCIceCandidate | null): void {
    if (candidate === null) {
      // Конец сбора: ждать больше нечего.
      this.flush()
      return
    }
    this.outgoing.push(candidate.toJSON())
    if (this.flushTimer === null) {
      this.flushTimer = this.timers.setTimeout(() => this.flush(), this.opts.candidateFlushMs ?? 150)
    }
  }

  private flush(): void {
    if (this.flushTimer !== null) {
      this.timers.clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (this.outgoing.length === 0 || this.closed) return
    const candidates = this.outgoing.splice(0)
    void this.opts.sendSignal({ type: "ice", candidates }).catch(() => {
      // Потерянная пачка не фатальна: путь найдётся по остальным кандидатам,
      // а звонок, у которого путей нет вовсе, завершит окно обрыва.
    })
  }

  // --- обрыв и восстановление ---------------------------------------------------

  private onStateChange(): void {
    const pc = this.pc
    if (pc === null || this.closed) return
    switch (pc.connectionState) {
      case "connected":
        this.clearReconnect()
        void this.reportConnected()
        return
      case "disconnected":
        this.startReconnect()
        // Короткий обрыв нередко проходит сам: перезапуск — через паузу.
        if (this.opts.role === "caller") {
          this.timers.setTimeout(() => {
            if (!this.closed && this.pc?.connectionState === "disconnected") {
              void this.enqueue(() => this.offer(true))
            }
          }, 2000)
        }
        return
      case "failed":
        this.startReconnect()
        if (this.opts.role === "caller") void this.enqueue(() => this.offer(true))
        return
      default:
        return
    }
  }

  private startReconnect(): void {
    if (this.reconnectTimer !== null) return
    this.reconnectTimer = this.timers.setTimeout(
      () => this.fail("failed"),
      this.opts.reconnectWindowMs ?? 15_000,
    )
  }

  private clearReconnect(): void {
    if (this.reconnectTimer === null) return
    this.timers.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
  }

  private async reportConnected(): Promise<void> {
    if (this.reported || this.pc === null) return
    this.reported = true
    this.capBitrate()
    let type: ConnectionType = "direct"
    try {
      type = await connectionType(this.pc)
    } catch {
      // Не смогли измерить — считаем прямым: метрика получит `direct`, а не тишину.
    }
    if (!this.closed) this.opts.onConnected(type)
  }

  /** Потолок битрейта видео; не получилось — не повод ронять звонок. */
  private capBitrate(): void {
    const pc = this.pc
    if (pc === null) return
    for (const sender of pc.getSenders()) {
      if (sender.track?.kind !== "video") continue
      try {
        const params = sender.getParameters()
        const encodings = params.encodings?.length ? params.encodings : [{}]
        encodings[0] = { ...encodings[0], maxBitrate: VIDEO_MAX_BITRATE }
        void sender.setParameters({ ...params, encodings }).catch(() => undefined)
      } catch {
        // см. выше
      }
    }
  }

  // --- управление медиа -----------------------------------------------------------

  setMuted(muted: boolean): void {
    this.local?.getAudioTracks().forEach((track) => {
      track.enabled = !muted
    })
  }

  setCameraOff(off: boolean): void {
    this.local?.getVideoTracks().forEach((track) => {
      track.enabled = !off
    })
  }

  hasVideo(): boolean {
    return (this.local?.getVideoTracks().length ?? 0) > 0
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    this.clearReconnect()
    if (this.flushTimer !== null) this.timers.clearTimeout(this.flushTimer)
    this.local?.getTracks().forEach((track) => track.stop())
    if (this.pc !== null) {
      this.pc.ontrack = null
      this.pc.onicecandidate = null
      this.pc.onconnectionstatechange = null
      this.pc.close()
    }
  }
}

/** Путь медиа по `getStats()`: `relay`, если любой конец идёт через TURN. */
export async function connectionType(pc: RTCPeerConnection): Promise<ConnectionType> {
  const report = await pc.getStats()
  const byId = new Map<string, Record<string, unknown>>()
  report.forEach((value: Record<string, unknown>, key: string) => byId.set(key, value))

  let pair: Record<string, unknown> | undefined
  for (const value of byId.values()) {
    if (value["type"] === "transport" && typeof value["selectedCandidatePairId"] === "string") {
      pair = byId.get(value["selectedCandidatePairId"])
    }
  }
  if (pair === undefined) {
    for (const value of byId.values()) {
      if (value["type"] === "candidate-pair" && (value["nominated"] === true || value["selected"] === true)) {
        pair = value
      }
    }
  }
  if (pair === undefined) return "direct"
  const local = byId.get(String(pair["localCandidateId"]))
  const remote = byId.get(String(pair["remoteCandidateId"]))
  return local?.["candidateType"] === "relay" || remote?.["candidateType"] === "relay" ? "relay" : "direct"
}
