/**
 * Звонки на уровне приложения: соединение сигнализации, движок WebRTC и
 * состояние экрана — **над** страницей беседы.
 *
 * Над страницей, а не в ней, потому что звонок не принадлежит беседе, которая
 * сейчас открыта: человек переключает беседы посреди разговора, и движок,
 * живущий внутри страницы, был бы пересоздан вместе с ней — то есть звонок
 * оборвался бы от переключения. Поэтому провайдер монтируется один раз на
 * вход и отдаёт страницам действия через контекст.
 *
 * Правду о звонке хранит сервер. Здесь — только отражение: события и ответы
 * проходят через `callReducer`, который отбрасывает устаревшее по номерам.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"

import { ApiProblem } from "../../api/problems"
import { callActionFromUrl, listenForCallAction, type PendingCallAction } from "./callActions"
import { browserAlertEnv, createIncomingAlert, type AlertEnv } from "./incomingAlert"
import { isRecord } from "../messages/message-adapter"
import { createCallChannelClient, type CentrifugeFactory } from "../realtime/realtimeClient"
import {
  CallEngine,
  parseVideoQuality,
  type EngineEnv,
  type VideoQuality,
  type WireSignal,
} from "./callEngine"
import { createSerialQueue, withRetry } from "./signalQueue"
import {
  IDLE,
  callReducer,
  isLive,
  shouldApplySignal,
  type CallAction,
  type CallEndReason,
  type CallKind,
  type CallView,
} from "./callState"
import type { CallsOperations } from "./callsApi"

/** Сколько на экране висит итог звонка, прежде чем экран гаснет. */
export const ENDED_SCREEN_MS = 2500
/** Как часто клиент подтверждает, что звонок жив (сервер ждёт тишину до 90 с). */
export const KEEPALIVE_MS = 30_000

const QUALITY_KEY = "messenger.call.videoQuality"

function forceRelay(): boolean {
  try {
    return window.localStorage.getItem("messenger.call.forceRelay") === "1"
  } catch {
    return false
  }
}

/** Выбор человека живёт в браузере: удобство, а не состояние (может не прочитаться). */
function loadVideoQuality(): VideoQuality {
  try {
    return parseVideoQuality(window.localStorage.getItem(QUALITY_KEY))
  } catch {
    return "auto"
  }
}

export interface CallsContextValue {
  readonly view: CallView
  readonly localStream: MediaStream | null
  readonly remoteStream: MediaStream | null
  /** Соединение сигнализации поднято. Без него звонить и принимать нельзя. */
  readonly signalingUp: boolean
  /** Браузер умеет WebRTC и страница в безопасном контексте. */
  readonly supported: boolean
  /** Короткое сообщение об отказе начать звонок; гаснет само. */
  readonly notice: string | null
  /** Качество **исходящего** видео: `auto` или пресет. */
  readonly videoQuality: VideoQuality
  startCall(conversationId: string, kind: CallKind, peerName: string): void
  accept(): void
  decline(): void
  hangup(): void
  setMuted(muted: boolean): void
  setCameraOff(off: boolean): void
  setVideoQuality(quality: VideoQuality): void
  hasVideo(): boolean
}

/** Экспортируется для тестов и историй интерфейса; в приложении — только через провайдер. */
export const CallsContext = createContext<CallsContextValue | null>(null)

/** `null`, когда звонков нет (выключены на окружении): интерфейс кнопок не рисует. */
export function useCalls(): CallsContextValue | null {
  return useContext(CallsContext)
}

export function browserEngineEnv(): EngineEnv | null {
  if (
    typeof window === "undefined" ||
    !window.isSecureContext ||
    typeof RTCPeerConnection === "undefined" ||
    !navigator.mediaDevices?.getUserMedia
  ) {
    return null
  }
  return {
    getUserMedia: (constraints) => navigator.mediaDevices.getUserMedia(constraints),
    createPeer: (config) => new RTCPeerConnection(config),
  }
}

interface CallsProviderProps {
  readonly ops: CallsOperations
  readonly centrifugoUrl: string
  /** Билет **на канал звонков**: `POST /realtime/token?scope=calls`. */
  readonly issueCallsTicket: () => Promise<string>
  readonly viewerId: string
  /** Имя собеседника по беседе — для звонка, восстановленного после перезагрузки. */
  readonly peerNameFor: (conversationId: string) => string | null
  readonly children: ReactNode
  /** Тестовые швы. */
  readonly env?: EngineEnv | null
  readonly createCentrifuge?: CentrifugeFactory
  /** Среда сигнала о входящем звонке (звук, заголовок, уведомление); по умолчанию — браузер. */
  readonly alertEnv?: AlertEnv
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null
}

function kindOf(value: unknown): CallKind | null {
  return value === "audio" || value === "video" ? value : null
}

function toWire(signal: unknown): WireSignal | null {
  if (!isRecord(signal)) return null
  if ((signal.type === "offer" || signal.type === "answer") && typeof signal.sdp === "string") {
    return { type: signal.type, sdp: signal.sdp }
  }
  if (signal.type === "ice" && Array.isArray(signal.candidates)) {
    return { type: "ice", candidates: signal.candidates.filter(isRecord) as RTCIceCandidateInit[] }
  }
  return null
}

export function CallsProvider({
  ops,
  centrifugoUrl,
  issueCallsTicket,
  viewerId,
  peerNameFor,
  children,
  env: envProp,
  createCentrifuge,
  alertEnv,
}: CallsProviderProps) {
  const env = useMemo(() => (envProp === undefined ? browserEngineEnv() : envProp), [envProp])

  const viewRef = useRef<CallView>(IDLE)
  const [view, setView] = useState<CallView>(IDLE)
  const [localStream, setLocalStream] = useState<MediaStream | null>(null)
  const [remoteStream, setRemoteStream] = useState<MediaStream | null>(null)
  const [signalingUp, setSignalingUp] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [videoQuality, setVideoQualityState] = useState<VideoQuality>(loadVideoQuality)
  const videoQualityRef = useRef(videoQuality)

  // Метка этой вкладки. Две вкладки вызываемого, нажавшие «принять» одновременно,
  // обе подняли бы медиа и обе ответили бы на `offer`; по метке сервер называет
  // победителя, а проигравшая сворачивается, не трогая звонок.
  const tabIdRef = useRef<string>(crypto.randomUUID())

  const engineRef = useRef<CallEngine | null>(null)
  // Идентификаторы уже применённых сигналов текущего звонка: повтор после потерянного
  // ответа приходит с тем же `signal_id` и вторым `offer` запустил бы пересогласование.
  const appliedSignalIdsRef = useRef<Set<string>>(new Set())
  const keepaliveRef = useRef<number | null>(null)
  const wakeLockRef = useRef<{ release(): Promise<void> } | null>(null)

  const apply = useCallback((action: CallAction): CallView => {
    const next = callReducer(viewRef.current, action)
    if (next !== viewRef.current) {
      viewRef.current = next
      setView(next)
    }
    return next
  }, [])

  const stopKeepalive = useCallback(() => {
    if (keepaliveRef.current !== null) {
      window.clearInterval(keepaliveRef.current)
      keepaliveRef.current = null
    }
  }, [])

  const releaseWakeLock = useCallback(() => {
    void wakeLockRef.current?.release().catch(() => undefined)
    wakeLockRef.current = null
  }, [])

  const teardown = useCallback(() => {
    engineRef.current?.close()
    engineRef.current = null
    appliedSignalIdsRef.current.clear()
    stopKeepalive()
    releaseWakeLock()
    setLocalStream(null)
    setRemoteStream(null)
  }, [stopKeepalive, releaseWakeLock])

  // Экран на телефоне гаснет сам и роняет звонок: пока идёт разговор, держим его.
  const acquireWakeLock = useCallback(() => {
    const lock = (navigator as Navigator & { wakeLock?: { request(type: "screen"): Promise<{ release(): Promise<void> }> } }).wakeLock
    if (lock === undefined) return
    void lock
      .request("screen")
      .then((sentinel) => {
        wakeLockRef.current = sentinel
      })
      .catch(() => undefined)
  }, [])

  const startKeepalive = useCallback(
    (callId: string) => {
      stopKeepalive()
      keepaliveRef.current = window.setInterval(() => {
        ops
          .keepalive(callId)
          .then((call) => {
            if (call.state === "ended") {
              const prev = viewRef.current
              const next = apply({
                type: "state",
                callId,
                state: "ended",
                reason: call.endReason,
                version: call.version,
              })
              if (prev.phase !== "ended" && next.phase === "ended") teardown()
            }
          })
          .catch(() => {
            // Одна потерянная проверка не конец звонка: сервер ждёт тишину 90 с.
          })
      }, KEEPALIVE_MS)
    },
    [ops, apply, stopKeepalive, teardown],
  )

  const endLocally = useCallback(
    (reason: CallEndReason) => {
      const callId = viewRef.current.callId
      apply({ type: "local-ended", reason })
      teardown()
      if (callId === null) return
      // Обрыв — отдельный переход `fail`, а не `hangup`: трубка в активном звонке
      // записывается сервером как состоявшийся разговор (`completed`), и сбой
      // сети попадал бы в ленту, метрики и долю неудач разговором.
      const failed = reason === "failed" || reason === "media_denied"
      void (failed ? ops.fail(callId) : ops.hangup(callId)).catch(() => undefined)
    },
    [apply, teardown, ops],
  )

  /** Поднимает движок, когда звонок принят, — один раз за звонок. */
  const ensureEngine = useCallback((after?: Promise<unknown>) => {
    const current = viewRef.current
    if (engineRef.current !== null || env === null || current.callId === null || current.role === null) return
    const callId = current.callId
    const engine = new CallEngine({
      env,
      role: current.role,
      kind: current.kind,
      // Выбранное раньше качество действует с первой секунды, а не только после смены.
      videoQuality: videoQualityRef.current,
      // На устройствах Apple H.264 кодируется аппаратно — «как в айфонах».
      preferH264: /Mac OS X|iPhone|iPad|iPod/.test(navigator.userAgent),
      // Отладочный переключатель проверки TURN: `localStorage["messenger.call.forceRelay"]="1"`
      // заставляет соединение идти только через релей. В интерфейсе его нет намеренно.
      ...(forceRelay() ? { iceTransportPolicy: "relay" as const } : {}),
      // Сигналы уходят **строго по одному**: `offer` и пачка кандидатов,
      // отправленные параллельно, достигают сервера в любом порядке, а номер им
      // выдаётся по порядку прихода — и `offer` получал бы номер больше, чем
      // кандидаты, пришедшие раньше.
      sendSignal: createSerialQueue<WireSignal>((signal) => {
        // Один `signalId` на логический сигнал, общий для всех повторов: если ответ
        // потерялся, а сигнал дошёл, получатель отбросит дубль по нему.
        const withId = { ...signal, signalId: crypto.randomUUID() }
        return withRetry(() => ops.signal(callId, withId), {
          // Отказ клиента (4xx) повторять бессмысленно; сеть и 5xx — стоит.
          retryable: (error) => !(error instanceof ApiProblem && error.status < 500),
        })
      }),
      // Данные TURN выдаются только **принятому** звонку. Вызываемый поднимает
      // движок в момент нажатия, до ответа сервера на «принять», поэтому его
      // запрос данных ждёт этого ответа (`after`) — иначе он получал бы
      // `call_not_ready`.
      iceServers: async () => {
        if (after !== undefined) await after
        return ops.iceServers(callId)
      },
      onLocalStream: setLocalStream,
      onRemoteStream: setRemoteStream,
      onConnected: (type) => {
        apply({ type: "connected" })
        // Вызывается при **каждой** смене пути (прямой → релейный после смены сети).
        void ops.connected(callId, type).catch(() => undefined)
        if (keepaliveRef.current === null) startKeepalive(callId)
        if (wakeLockRef.current === null) acquireWakeLock()
      },
      onFailed: (reason) => endLocally(reason === "media_denied" ? "media_denied" : "failed"),
    })
    engineRef.current = engine
    void engine.start()
  }, [env, ops, apply, startKeepalive, acquireWakeLock, endLocally])

  // --- события сигнализации ------------------------------------------------------

  const handleEvent = useCallback(
    (payload: unknown) => {
      if (!isRecord(payload)) return
      const callId = str(payload.call_id)
      if (callId === null) return

      switch (payload.type) {
        case "call.incoming": {
          const conversationId = str(payload.conversation_id)
          const kind = kindOf(payload.kind)
          const version = num(payload.version)
          const caller = isRecord(payload.caller) ? payload.caller : {}
          if (conversationId === null || kind === null || version === null) return
          apply({
            type: "incoming",
            callId,
            conversationId,
            kind,
            peerName: str(caller.display_name) ?? peerNameFor(conversationId) ?? "Unknown",
            version,
          })
          return
        }

        case "call.state": {
          const state = payload.state
          const version = num(payload.version)
          if (
            version === null ||
            (state !== "ringing" && state !== "accepted" && state !== "active" && state !== "ended")
          ) {
            return
          }
          const prev = viewRef.current
          const next = apply({
            type: "state",
            callId,
            state,
            reason: str(payload.reason),
            version,
          })
          if (next.phase === "ended" && prev.phase !== "ended") teardown()
          else if (next.phase === "connecting") ensureEngine()
          return
        }

        case "call.signal": {
          const seq = num(payload.seq)
          const wire = toWire(payload.signal)
          if (seq === null || wire === null) return
          // Сигнал чужого звонка и устаревший отбрасываются здесь (`CALL-007`).
          if (!shouldApplySignal(viewRef.current, callId, seq)) return
          const signalId = str(payload.signal_id)
          if (signalId !== null) {
            if (appliedSignalIdsRef.current.has(signalId)) return
            appliedSignalIdsRef.current.add(signalId)
          }
          apply({ type: "signal-applied", seq })
          void engineRef.current?.handleSignal(wire)
          return
        }
      }
    },
    [apply, ensureEngine, peerNameFor, teardown],
  )

  const handleEventRef = useRef(handleEvent)
  useEffect(() => {
    handleEventRef.current = handleEvent
  }, [handleEvent])

  /**
   * Сверка с сервером. У канала звонков **нет истории**: событие, пришедшее, пока
   * соединение было оборвано, потеряно навсегда, и единственный путь вернуть
   * истину — спросить. Поэтому сверка идёт при **каждом** подъёме соединения, а не
   * только при открытии страницы.
   *
   * Чего здесь **нет**, и это названо: вкладка, застающая чужой идущий звонок,
   * его **не завершает**. Прежняя редакция делала `hangup` на любой «живой, но не
   * мой» звонок, считая, что прежняя страница исчезла, — и новая вкладка,
   * открытая посреди разговора, обрывала его в первой. Владелец медиа — вкладка,
   * в которой работает движок; остальные молчат. Брошенный звонок (страница
   * закрыта, медиа пропало) закрывает не новая вкладка, а закрытие страницы
   * (`pagehide`) или тишина `keepalive` на сервере.
   */
  const reconcile = useCallback(async () => {
    let call
    try {
      call = await ops.current()
    } catch {
      return
    }
    const view = viewRef.current
    if (call === null) {
      // Сервер звонка не знает, а у нас он идёт: конец пропал вместе с событием.
      if (isLive(view)) {
        apply({ type: "local-ended", reason: "failed" })
        teardown()
      }
      return
    }
    if (!isLive(view)) {
      // Свободная вкладка: показываем только входящий, который ещё звонит. Всё
      // остальное ведёт другая вкладка.
      if (call.state === "ringing" && call.role === "callee") {
        apply({ type: "api-call", call, peerName: peerNameFor(call.conversationId) })
      }
      return
    }
    if (view.callId !== call.callId) return
    const prev = view
    const next = apply({ type: "api-call", call, peerName: null })
    if (next.phase === "ended" && prev.phase !== "ended") teardown()
    else if (next.phase === "connecting") ensureEngine()
  }, [ops, apply, peerNameFor, ensureEngine, teardown])

  const reconcileRef = useRef(reconcile)
  useEffect(() => {
    reconcileRef.current = reconcile
  }, [reconcile])

  // Соединение сигнализации живёт, пока открыто приложение, и не зависит от того,
  // какая беседа на экране (см. `createCallChannelClient`).
  useEffect(() => {
    const client = createCallChannelClient({
      centrifugoUrl,
      channel: `call:${viewerId}`,
      issueTicket: issueCallsTicket,
      onPublication: (payload) => handleEventRef.current(payload),
      onConnectionChange: (connected) => {
        setSignalingUp(connected)
        if (connected) void reconcileRef.current()
      },
      ...(createCentrifuge === undefined ? {} : { createCentrifuge }),
    })
    client.start()
    return () => client.stop()
  }, [centrifugoUrl, viewerId, issueCallsTicket, createCentrifuge])

  // Подстраховка сверкой, пока звонок звонит или соединяется. Событие «принято» —
  // единственное, по чему звонящий начинает `offer`; у канала нет истории, и пока
  // соединение звонков живо, потерянное событие иначе не вернуть. Раз в несколько
  // секунд спросить сервер дёшево, а зависший звонок стоит тридцати секунд гудков.
  useEffect(() => {
    if (view.phase !== "outgoing" && view.phase !== "connecting") return
    const id = window.setInterval(() => void reconcileRef.current(), 5000)
    return () => window.clearInterval(id)
  }, [view.phase, view.callId])

  // Страница с идущим звонком закрывается или перезагружается: медиа живёт в ней и
  // вместе с ней пропадёт, поэтому звонок завершается сразу, а не через 90 секунд
  // тишины. `keepalive` оставляет запрос в живых после выгрузки страницы.
  useEffect(() => {
    const onPageHide = () => {
      const current = viewRef.current
      if (engineRef.current === null || current.callId === null || !isLive(current)) return
      void ops.hangup(current.callId, { unload: true }).catch(() => undefined)
    }
    window.addEventListener("pagehide", onPageHide)
    return () => window.removeEventListener("pagehide", onPageHide)
  }, [ops])

  // Итог звонка висит на экране недолго.
  useEffect(() => {
    if (view.phase !== "ended") return
    const id = window.setTimeout(() => apply({ type: "reset" }), ENDED_SCREEN_MS)
    return () => window.clearTimeout(id)
  }, [view.phase, view.callId, apply])

  // Короткое сообщение гаснет само.
  useEffect(() => {
    if (notice === null) return
    const id = window.setTimeout(() => setNotice(null), 4000)
    return () => window.clearTimeout(id)
  }, [notice])

  // Вкладка закрывается — движок и таймеры не должны пережить её.
  useEffect(() => () => teardown(), [teardown])

  // --- действия человека ------------------------------------------------------------

  const startCall = useCallback(
    (conversationId: string, kind: CallKind, peerName: string) => {
      if (env === null) {
        setNotice("Calls aren't supported in this browser.")
        return
      }
      if (viewRef.current.phase === "ended") apply({ type: "reset" })
      if (isLive(viewRef.current)) return
      void ops
        .start(conversationId, kind)
        .then((call) => {
          const next = apply({ type: "api-call", call, peerName })
          if (next.phase === "connecting") ensureEngine()
        })
        .catch(() => setNotice("Couldn't start the call. Try again."))
    },
    [env, ops, apply, ensureEngine],
  )

  const accept = useCallback(() => {
    const current = viewRef.current
    if (current.phase !== "incoming" || current.callId === null) return
    const callId = current.callId
    apply({ type: "local-accepting" })
    const accepting = ops.accept(callId, tabIdRef.current)
    // Движок поднимается **сразу**, а не после ответа сервера: `offer` звонящего
    // придёт, как только сервер примет «принять», и ему нужно, чтобы движок уже
    // был, иначе сигнал уйдёт в никуда. Данные TURN он берёт после этого ответа.
    ensureEngine(accepting)
    void accepting
      .then((call) => apply({ type: "api-call", call, peerName: null }))
      .catch((error: unknown) => {
        // Проиграли гонку «принять»: звонок принят другой вкладкой, и **он идёт**.
        // Сворачиваемся, не завершая его, — `hangup` отсюда оборвал бы разговор
        // победительницы.
        const taken = error instanceof ApiProblem && error.code === "call_taken"
        apply({ type: "local-ended", reason: taken ? "accepted_elsewhere" : "failed" })
        teardown()
      })
  }, [apply, ensureEngine, ops, teardown])

  const decline = useCallback(() => {
    const current = viewRef.current
    if (current.phase !== "incoming" || current.callId === null) return
    void ops.decline(current.callId).catch(() => undefined)
    apply({ type: "local-ended", reason: "declined" })
  }, [apply, ops])

  const hangup = useCallback(() => {
    const current = viewRef.current
    if (!isLive(current)) return
    endLocally(current.phase === "active" || current.phase === "connecting" ? "completed" : "cancelled")
  }, [endLocally])

  const setMuted = useCallback(
    (muted: boolean) => {
      engineRef.current?.setMuted(muted)
      apply({ type: "set-muted", muted })
    },
    [apply],
  )

  const setCameraOff = useCallback(
    (off: boolean) => {
      engineRef.current?.setCameraOff(off)
      apply({ type: "set-camera-off", cameraOff: off })
    },
    [apply],
  )

  const setVideoQuality = useCallback((quality: VideoQuality) => {
    videoQualityRef.current = quality
    setVideoQualityState(quality)
    engineRef.current?.setVideoQuality(quality)
    try {
      window.localStorage.setItem(QUALITY_KEY, quality)
    } catch {
      // Не сохранилось — выбор живёт до закрытия вкладки.
    }
  }, [])

  const hasVideo = useCallback(() => engineRef.current?.hasVideo() ?? false, [])

  // --- сигнал о входящем звонке ------------------------------------------------------

  const alert = useMemo(() => createIncomingAlert(alertEnv ?? browserAlertEnv()), [alertEnv])

  useEffect(() => {
    if (view.phase === "incoming" && view.callId !== null) {
      alert.start({ callId: view.callId, name: view.peerName ?? "Unknown", kind: view.kind })
    } else {
      alert.stop()
    }
    return () => alert.stop()
  }, [alert, view.phase, view.callId, view.peerName, view.kind])

  // Действие с уведомления («Принять», «Отклонить») ждёт, пока звонок появится на
  // экране: из закрытой вкладки приложение открывается раньше, чем сигнализация
  // успевает показать входящий. Не дождалось за минуту — отбрасывается: принять
  // звонок, которого уже нет, нельзя.
  const pendingRef = useRef<PendingCallAction | null>(null)
  const runPending = useCallback(() => {
    const pending = pendingRef.current
    const current = viewRef.current
    if (pending === null || current.callId !== pending.callId || current.phase !== "incoming") return
    pendingRef.current = null
    if (pending.action === "accept") acceptRef.current()
    else if (pending.action === "decline") declineRef.current()
  }, [])

  const acceptRef = useRef(accept)
  const declineRef = useRef(decline)
  useEffect(() => {
    acceptRef.current = accept
    declineRef.current = decline
  }, [accept, decline])

  useEffect(() => {
    let expire: number | null = null
    const take = (pending: PendingCallAction) => {
      pendingRef.current = pending
      if (expire !== null) window.clearTimeout(expire)
      expire = window.setTimeout(() => (pendingRef.current = null), 60_000)
      runPending()
    }
    const fromUrl = callActionFromUrl(window.location.search)
    if (fromUrl !== null) {
      take(fromUrl)
      // Адрес чистится: перезагрузка страницы не должна принять звонок второй раз.
      const url = new URL(window.location.href)
      url.searchParams.delete("call")
      url.searchParams.delete("action")
      window.history.replaceState(null, "", url.toString())
    }
    const stop = listenForCallAction(take)
    return () => {
      stop()
      if (expire !== null) window.clearTimeout(expire)
    }
  }, [runPending])

  useEffect(() => {
    runPending()
  }, [view.phase, view.callId, runPending])

  const value = useMemo<CallsContextValue>(
    () => ({
      view,
      localStream,
      remoteStream,
      signalingUp,
      supported: env !== null,
      notice,
      videoQuality,
      startCall,
      accept,
      decline,
      hangup,
      setMuted,
      setCameraOff,
      setVideoQuality,
      hasVideo,
    }),
    [
      view, localStream, remoteStream, signalingUp, env, notice, videoQuality, startCall, accept,
      decline, hangup, setMuted, setCameraOff, setVideoQuality, hasVideo,
    ],
  )

  return <CallsContext.Provider value={value}>{children}</CallsContext.Provider>
}
