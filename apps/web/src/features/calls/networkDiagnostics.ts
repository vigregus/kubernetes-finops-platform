import { filterTurnTransport, type ConnectionDetail } from "./callEngine"

/**
 * Диагностика сети по событию (`RES-012`, часть 18, §7).
 *
 * Не сканер и не детектор блокировок: клиент проверяет, что **достижимо ему сейчас**, и сообщает
 * серверу технический результат в закрытых наборах — ни адресов, ни имён, ни идентификаторов.
 * Две связанные точки: «до» (`failure`) и «после» (`recovery`, что спасло звонок). Любой сбой
 * диагностики — тишина: она не вправе ни ронять звонок, ни задерживать его.
 */
export type ProbeName = "https" | "websocket" | "sse" | "stun" | "turn_udp" | "turn_tcp" | "turn_tls"
export type ProbeVerdict = "ok" | "failed" | "timeout" | "skipped"
export interface ProbeResult {
  readonly probe: ProbeName
  readonly result: ProbeVerdict
}

export type DiagnosticTrigger =
  | "ice_slow"
  | "ice_failed"
  | "allocation_timeout"
  | "realtime_failed"
  | "fallback_enabled"
  | "network_change"
  | "setup_timeout"

export interface DiagnosticReport {
  readonly phase: "failure" | "recovery"
  readonly trigger: DiagnosticTrigger
  readonly setupResult: "connected" | "failed" | "timeout"
  readonly probes?: readonly ProbeResult[]
  readonly iceGatheringMs?: number
  readonly iceConnectionMs?: number
  readonly selectedCandidate?: "host" | "srflx" | "prflx" | "relay"
  readonly selectedRelayProtocol?: "udp" | "tcp" | "tls"
  readonly failureReason?: string
}

export interface DiagnosticSession {
  readonly diagnosticId: string
}

export interface DiagnosticsOps {
  session(): Promise<DiagnosticSession>
  report(diagnosticId: string, report: DiagnosticReport): Promise<void>
}

/** Всё, что нужно пробам от окружения: подменяется в тестах. */
export interface ProbeEnv {
  createPeer(config: RTCConfiguration): RTCPeerConnection
  /** Достучался ли браузер до собственного origin по HTTPS. */
  https(): Promise<boolean>
  /** Соединение реального времени поднято; `null` — неизвестно. */
  websocketUp(): boolean | null
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(id: unknown): void
}

export const PROBE_TIMEOUT_MS = 5_000

/** Пробы `Allocate`/STUN: отдельное соединение на пробу, кандидатов ждём до срока. */
async function gather(
  env: ProbeEnv,
  config: RTCConfiguration,
  wanted: "srflx" | "relay",
  timeoutMs: number,
): Promise<ProbeVerdict> {
  let pc: RTCPeerConnection
  try {
    pc = env.createPeer(config)
  } catch {
    return "failed"
  }
  return new Promise<ProbeVerdict>((resolve) => {
    let done = false
    let timer: unknown = null
    const finish = (verdict: ProbeVerdict) => {
      if (done) return
      done = true
      if (timer !== null) env.clearTimeout(timer)
      pc.onicecandidate = null
      try {
        pc.close()
      } catch {
        // уже закрыто
      }
      resolve(verdict)
    }
    pc.onicecandidate = (event) => {
      // Конец сбора без нужного кандидата — отказ, а не молчание до срока.
      if (event.candidate === null) return finish("failed")
      if (event.candidate.type === wanted) finish("ok")
    }
    timer = env.setTimeout(() => finish("timeout"), timeoutMs)
    try {
      pc.createDataChannel("diagnostic")
      void pc
        .createOffer()
        .then((offer) => pc.setLocalDescription(offer))
        .catch(() => finish("failed"))
    } catch {
      finish("failed")
    }
  })
}

/** Пробы, которым не нужно выделение на TURN: HTTPS, реальное время, SSE, STUN. */
export async function runBasicProbes(
  env: ProbeEnv,
  servers: readonly RTCIceServer[],
  timeoutMs = PROBE_TIMEOUT_MS,
): Promise<ProbeResult[]> {
  const stunServers = servers
    .map((server) => ({
      ...server,
      urls: (Array.isArray(server.urls) ? server.urls : [server.urls]).filter((url) =>
        url.startsWith("stun"),
      ),
    }))
    .filter((server) => server.urls.length > 0)
  const websocket = env.websocketUp()
  const [https, stun] = await Promise.all([
    env.https().then(
      (ok): ProbeVerdict => (ok ? "ok" : "failed"),
      (): ProbeVerdict => "failed",
    ),
    stunServers.length === 0
      ? Promise.resolve<ProbeVerdict>("skipped")
      : gather(env, { iceServers: stunServers }, "srflx", timeoutMs),
  ])
  return [
    { probe: "https", result: https },
    { probe: "websocket", result: websocket === null ? "skipped" : websocket ? "ok" : "failed" },
    { probe: "sse", result: "skipped" },
    { probe: "stun", result: stun },
  ]
}

/**
 * `turn_*` проверяют настоящий `Allocate` (кандидат `relay` под `iceTransportPolicy=relay`
 * появляется только после успешного выделения), а не «порт открыт». **По одной**, а не
 * параллельно: у coturn `--user-quota` на пользователя, и три одновременных выделения
 * диагностики вместе с выделениями живого звонка могли бы выбрать квоту и изменить исход
 * звонка, который диагностируют. Наблюдатель не вправе влиять на наблюдаемое.
 */
export async function runTurnProbes(
  env: ProbeEnv,
  servers: readonly RTCIceServer[],
  timeoutMs = PROBE_TIMEOUT_MS,
): Promise<ProbeResult[]> {
  const transport = (name: "udp" | "tcp" | "tls"): Promise<ProbeVerdict> => {
    const filtered = filterTurnTransport([...servers], name).filter((server) =>
      (Array.isArray(server.urls) ? server.urls : [server.urls]).some((url) => url.startsWith("turn")),
    )
    if (filtered.length === 0) return Promise.resolve("skipped")
    return gather(env, { iceServers: filtered, iceTransportPolicy: "relay" }, "relay", timeoutMs)
  }
  const out: ProbeResult[] = []
  for (const [probe, name] of [
    ["turn_udp", "udp"],
    ["turn_tcp", "tcp"],
    ["turn_tls", "tls"],
  ] as const) {
    out.push({ probe, result: await transport(name) })
  }
  return out
}

/** Все пробы: сначала базовые, затем TURN по одной. `sse` пока `skipped` (`RES-001..003`). */
export async function runProbes(
  env: ProbeEnv,
  servers: readonly RTCIceServer[],
  timeoutMs = PROBE_TIMEOUT_MS,
): Promise<ProbeResult[]> {
  return [
    ...(await runBasicProbes(env, servers, timeoutMs)),
    ...(await runTurnProbes(env, servers, timeoutMs)),
  ]
}

/** Сколько ждать исхода звонка перед активными TURN-пробами, мс. */
export const SETTLE_WAIT_MS = 60_000

/**
 * Одна диагностика на звонок: «до» (`begin`) и «после» (`recovered`). Идентификатор сессии живёт
 * только в памяти. Неудача сессии (429, сеть) — тишина и без повторов.
 */
export class CallDiagnostics {
  private id: string | null = null
  private started = false
  private reportedRecovery = false
  private failureSent = false
  private pending: { trigger: DiagnosticTrigger; detail: ConnectionDetail } | null = null

  private readonly ops: DiagnosticsOps
  private readonly env: ProbeEnv
  private readonly servers: () => Promise<readonly RTCIceServer[]>
  private settled: Promise<void>
  private settleNow: () => void = () => {}

  /** `servers` — данные ICE текущего звонка: своих учётных данных диагностика не получает. */
  constructor(ops: DiagnosticsOps, env: ProbeEnv, servers: () => Promise<readonly RTCIceServer[]>) {
    this.ops = ops
    this.env = env
    this.servers = servers
    this.settled = new Promise<void>((resolve) => {
      this.settleNow = resolve
    })
  }

  /**
   * Звонок определился: соединился или окончательно завершён. До этого активные TURN-пробы не
   * стартуют — реальный ICE restart имеет приоритет над диагностикой (квота TURN общая).
   */
  settle(): void {
    this.settleNow()
  }

  private waitSettled(): Promise<void> {
    return new Promise<void>((resolve) => {
      const timer = this.env.setTimeout(resolve, SETTLE_WAIT_MS)
      void this.settled.then(() => {
        this.env.clearTimeout(timer)
        resolve()
      })
    })
  }

  /** Снимок «до»: пробы и отчёт `failure`. Повторный вызов на тот же звонок — пустой. */
  async begin(
    trigger: DiagnosticTrigger,
    setupResult: "failed" | "timeout" = "failed",
    failureReason?: string,
  ): Promise<void> {
    if (this.started) return
    this.started = true
    try {
      const session = await this.ops.session()
      this.id = session.diagnosticId
      const servers = await this.servers()
      // Базовые пробы (HTTPS, STUN) не занимают TURN и идут сразу; выделения — после исхода звонка.
      const basic = await runBasicProbes(this.env, servers)
      await this.waitSettled()
      const probes = [...basic, ...(await runTurnProbes(this.env, servers))]
      await this.ops.report(session.diagnosticId, {
        phase: "failure",
        trigger,
        setupResult,
        probes,
        ...(failureReason === undefined ? {} : { failureReason }),
      })
      // «После» не обгоняет «до»: звонок мог соединиться, пока шли пробы, — оно ждало здесь.
      this.failureSent = true
      await this.flushRecovery()
    } catch {
      // диагностика не должна быть причиной новых проблем
    }
  }

  /**
   * Снимок «после»: что именно спасло звонок. Быстрый fallback соединяется раньше, чем
   * открылась сессия диагностики, поэтому «после» не теряется, а ждёт идентификатор.
   * Без `begin` прикрепить его не к чему — тишина.
   */
  async recovered(trigger: DiagnosticTrigger, detail: ConnectionDetail): Promise<void> {
    if (!this.started || this.reportedRecovery) return
    this.pending = { trigger, detail }
    if (this.failureSent) await this.flushRecovery()
  }

  private async flushRecovery(): Promise<void> {
    const pending = this.pending
    if (pending === null || this.id === null || !this.failureSent || this.reportedRecovery) return
    this.pending = null
    this.reportedRecovery = true
    const { trigger, detail } = pending
    try {
      await this.ops.report(this.id, {
        phase: "recovery",
        trigger,
        setupResult: "connected",
        ...(detail.mediaPath === undefined ? {} : { selectedCandidate: detail.mediaPath }),
        ...(detail.turnTransport === undefined ? {} : { selectedRelayProtocol: detail.turnTransport }),
        ...(detail.setupMs === undefined ? {} : { iceConnectionMs: detail.setupMs }),
        ...(detail.gatheringMs === undefined ? {} : { iceGatheringMs: detail.gatheringMs }),
      })
    } catch {
      // см. выше
    }
  }
}
