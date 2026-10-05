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
  | { readonly type: "offer" | "answer"; readonly sdp: string; readonly signalId?: string }
  | {
      readonly type: "ice"
      readonly candidates: readonly RTCIceCandidateInit[]
      readonly signalId?: string
    }

export type ConnectionType = "direct" | "relay"

export interface EngineEnv {
  getUserMedia(constraints: MediaStreamConstraints): Promise<MediaStream>
  createPeer(config: RTCConfiguration): RTCPeerConnection
}

export interface IceConfig {
  readonly servers: RTCIceServer[]
  readonly ttlSeconds: number
}

/** За сколько до конца срока берутся новые данные TURN. */
export const ICE_REFRESH_LEAD_SECONDS = 60
/** Через сколько повторять неудавшееся обновление. */
export const ICE_REFRESH_RETRY_MS = 20_000

export interface EngineOptions {
  readonly env: EngineEnv
  readonly role: "caller" | "callee"
  readonly kind: "audio" | "video"
  readonly sendSignal: (signal: WireSignal) => Promise<void>
  /** Данные STUN/TURN и сколько они действуют: по истечении их нужно взять заново. */
  readonly iceServers: () => Promise<IceConfig>
  readonly onLocalStream: (stream: MediaStream) => void
  readonly onRemoteStream: (stream: MediaStream) => void
  /** Медиа пошло; путь соединения — по `getStats()`. Один раз за звонок. */
  readonly onConnected: (type: ConnectionType) => void
  /** Звонок не состоялся или оборвался: клиент завершает его. */
  readonly onFailed: (reason: "media_denied" | "failed") => void
  /** Начальное качество исходящего видео; по умолчанию `auto`. */
  readonly videoQuality?: VideoQuality
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

/**
 * Качество **исходящего** видео. Управляет тем, что человек отправляет, а не тем,
 * что он видит: чужую картинку определяет собеседник со своего экрана.
 *
 * `auto` — потолок оценки стоимости (1 Мбит/с) и адаптация самого браузера под
 * сеть; пресеты задают потолок явно. Пресет — не гарантия: при плохой сети браузер
 * опустит качество ниже заданного, а не выше.
 */
export type VideoQuality = "auto" | "low" | "medium" | "high"

export const VIDEO_QUALITIES: readonly VideoQuality[] = ["auto", "low", "medium", "high"]

interface VideoPreset {
  /** Во сколько раз уменьшить кадр (720p / 2 = 360p). */
  readonly scaleResolutionDownBy: number
  readonly maxBitrate: number
  readonly maxFramerate: number
}

/**
 * Разрешение считается от захвата 720p (`mediaConstraints`). `high` выше потолка
 * оценки стоимости — это выбор человека, а не умолчание.
 */
export const VIDEO_PRESETS: Readonly<Record<Exclude<VideoQuality, "auto">, VideoPreset>> = {
  low: { scaleResolutionDownBy: 2, maxBitrate: 350_000, maxFramerate: 15 },
  medium: { scaleResolutionDownBy: 1.5, maxBitrate: 700_000, maxFramerate: 24 },
  high: { scaleResolutionDownBy: 1, maxBitrate: 1_500_000, maxFramerate: 30 },
}

/** Что записать в кодирование отправителя для выбранного качества. */
export function encodingFor(quality: VideoQuality): RTCRtpEncodingParameters {
  if (quality === "auto") {
    return { maxBitrate: VIDEO_MAX_BITRATE, scaleResolutionDownBy: 1, maxFramerate: 30 }
  }
  return { ...VIDEO_PRESETS[quality] }
}

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
  private lastReported: ConnectionType | null = null
  private measuring = false
  private refreshTimer: unknown = null
  private quality: VideoQuality = "auto"

  constructor(options: EngineOptions) {
    this.opts = options
    this.quality = options.videoQuality ?? "auto"
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

      const ice = await this.opts.iceServers()
      const pc = this.opts.env.createPeer({ iceServers: ice.servers })
      this.pc = pc
      this.scheduleRefresh(ice.ttlSeconds)
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

  // --- обновление данных TURN ----------------------------------------------------

  /**
   * Данные TURN короткие (минуты), а звонок может быть долгим: за минуту до конца
   * срока берутся новые и ставятся в соединение (`setConfiguration`) — следующий
   * ICE restart и продление выделений идут уже по свежим. Раньше `iceServers`
   * задавались один раз при создании, и релейный звонок терял релей по истечении.
   * Неудача не фатальна: повтор через 20 с, пока звонок жив.
   */
  private scheduleRefresh(ttlSeconds: number, delayMs?: number): void {
    if (this.closed) return
    if (this.refreshTimer !== null) this.timers.clearTimeout(this.refreshTimer)
    const wait = delayMs ?? Math.max(ttlSeconds - ICE_REFRESH_LEAD_SECONDS, 30) * 1000
    this.refreshTimer = this.timers.setTimeout(() => void this.refreshIce(), wait)
  }

  private async refreshIce(): Promise<void> {
    this.refreshTimer = null
    const pc = this.pc
    if (pc === null || this.closed) return
    try {
      const ice = await this.opts.iceServers()
      if (this.closed) return
      pc.setConfiguration({ ...pc.getConfiguration(), iceServers: ice.servers })
      this.scheduleRefresh(ice.ttlSeconds)
    } catch {
      this.scheduleRefresh(0, ICE_REFRESH_RETRY_MS)
    }
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

  /**
   * Сообщает путь соединения при **каждом** установлении связи, а не один раз за звонок.
   *
   * Сеть меняется посреди звонка (Wi-Fi → мобильная): ICE перезапускается, и прямой
   * путь становится релейным. Одноразовое сообщение навсегда оставляло бы `direct`, и
   * доля релея — главная метрика стоимости — занижалась бы ровно на таких звонках.
   * Сообщается первое значение и любая **смена**; остальное — тишина.
   *
   * Пара кандидатов может быть ещё не выбрана в момент `connected` — тогда измерение
   * повторяется несколько раз; не вышло — первое сообщение всё равно уходит как
   * `direct`, чтобы метрика не молчала.
   */
  private async reportConnected(): Promise<void> {
    if (this.pc === null || this.measuring) return
    this.measuring = true
    try {
      let type: ConnectionType | null = null
      for (let attempt = 0; attempt < 4 && type === null && !this.closed; attempt += 1) {
        try {
          type = await connectionType(this.pc)
        } catch {
          type = null
        }
        if (type === null) await this.pause(500)
      }
      if (this.closed) return
      if (type === null) {
        if (this.lastReported !== null) return
        type = "direct"
      }
      const first = this.lastReported === null
      if (type === this.lastReported) return
      this.lastReported = type
      if (first) this.applyQuality()
      this.opts.onConnected(type)
    } finally {
      this.measuring = false
    }
  }

  private pause(ms: number): Promise<void> {
    return new Promise((resolve) => this.timers.setTimeout(resolve, ms))
  }

  /**
   * Применяет качество к отправителям видео: потолок битрейта, масштаб и частота
   * кадров. Не получилось — не повод ронять звонок (браузер без поддержки
   * `scaleResolutionDownBy` просто оставит своё).
   */
  private applyQuality(): void {
    const pc = this.pc
    if (pc === null) return
    for (const sender of pc.getSenders()) {
      if (sender.track?.kind !== "video") continue
      try {
        const params = sender.getParameters()
        const encodings = params.encodings?.length ? params.encodings : [{}]
        encodings[0] = { ...encodings[0], ...encodingFor(this.quality) }
        void sender.setParameters({ ...params, encodings }).catch(() => undefined)
      } catch {
        // см. выше
      }
    }
  }

  /** Меняет качество исходящего видео посреди звонка, без пересоздания соединения. */
  setVideoQuality(quality: VideoQuality): void {
    this.quality = quality
    this.applyQuality()
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
    if (this.refreshTimer !== null) this.timers.clearTimeout(this.refreshTimer)
    this.local?.getTracks().forEach((track) => track.stop())
    if (this.pc !== null) {
      this.pc.ontrack = null
      this.pc.onicecandidate = null
      this.pc.onconnectionstatechange = null
      this.pc.close()
    }
  }
}

/**
 * Путь медиа по `getStats()`: `relay`, если любой конец идёт через TURN. `null` —
 * выбранная пара ещё не определена (не путать с `direct`: это «пока неизвестно»).
 */
export async function connectionType(pc: RTCPeerConnection): Promise<ConnectionType | null> {
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
  if (pair === undefined) return null
  const local = byId.get(String(pair["localCandidateId"]))
  const remote = byId.get(String(pair["remoteCandidateId"]))
  return local?.["candidateType"] === "relay" || remote?.["candidateType"] === "relay" ? "relay" : "direct"
}
