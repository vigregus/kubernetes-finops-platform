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

import { isRecord } from "../messages/message-adapter"
import { createCallChannelClient, type CentrifugeFactory } from "../realtime/realtimeClient"
import { CallEngine, type EngineEnv, type WireSignal } from "./callEngine"
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
  startCall(conversationId: string, kind: CallKind, peerName: string): void
  accept(): void
  decline(): void
  hangup(): void
  setMuted(muted: boolean): void
  setCameraOff(off: boolean): void
  hasVideo(): boolean
}

const CallsContext = createContext<CallsContextValue | null>(null)

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
}: CallsProviderProps) {
  const env = useMemo(() => (envProp === undefined ? browserEngineEnv() : envProp), [envProp])

  const viewRef = useRef<CallView>(IDLE)
  const [view, setView] = useState<CallView>(IDLE)
  const [localStream, setLocalStream] = useState<MediaStream | null>(null)
  const [remoteStream, setRemoteStream] = useState<MediaStream | null>(null)
  const [signalingUp, setSignalingUp] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const engineRef = useRef<CallEngine | null>(null)
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
      if (callId !== null) void ops.hangup(callId).catch(() => undefined)
    },
    [apply, teardown, ops],
  )

  /** Поднимает движок, когда звонок принят, — один раз за звонок. */
  const ensureEngine = useCallback(() => {
    const current = viewRef.current
    if (engineRef.current !== null || env === null || current.callId === null || current.role === null) return
    const callId = current.callId
    const engine = new CallEngine({
      env,
      role: current.role,
      kind: current.kind,
      sendSignal: (signal) => ops.signal(callId, signal),
      iceServers: () => ops.iceServers(callId),
      onLocalStream: setLocalStream,
      onRemoteStream: setRemoteStream,
      onConnected: (type) => {
        apply({ type: "connected" })
        void ops.connected(callId, type).catch(() => undefined)
        startKeepalive(callId)
        acquireWakeLock()
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

  // Соединение сигнализации живёт, пока открыто приложение, и не зависит от того,
  // какая беседа на экране (см. `createCallChannelClient`).
  useEffect(() => {
    const client = createCallChannelClient({
      centrifugoUrl,
      channel: `call:${viewerId}`,
      issueTicket: issueCallsTicket,
      onPublication: (payload) => handleEventRef.current(payload),
      onConnectionChange: setSignalingUp,
      ...(createCentrifuge === undefined ? {} : { createCentrifuge }),
    })
    client.start()
    return () => client.stop()
  }, [centrifugoUrl, viewerId, issueCallsTicket, createCentrifuge])

  // Страница открылась посреди звонка (перезагрузка, новая вкладка). Входящий,
  // который ещё звонит, показываем; всё остальное оборвалось вместе с прежней
  // страницей — медиа живёт в ней, и подхватить его нечем, — поэтому завершаем.
  useEffect(() => {
    let cancelled = false
    void ops
      .current()
      .then((call) => {
        if (cancelled || call === null || isLive(viewRef.current)) return
        if (call.state === "ringing" && call.role === "callee") {
          apply({ type: "api-call", call, peerName: peerNameFor(call.conversationId) })
        } else {
          void ops.hangup(call.callId).catch(() => undefined)
        }
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [ops, apply, peerNameFor])

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
    // Движок поднимается **сразу**, а не после ответа сервера: `offer` звонящего
    // придёт, как только сервер примет «принять», и ему нужно, чтобы движок уже
    // был, иначе сигнал уйдёт в никуда.
    ensureEngine()
    void ops
      .accept(callId)
      .then((call) => apply({ type: "api-call", call, peerName: null }))
      .catch(() => {
        apply({ type: "local-ended", reason: "failed" })
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

  const hasVideo = useCallback(() => engineRef.current?.hasVideo() ?? false, [])

  const value = useMemo<CallsContextValue>(
    () => ({
      view,
      localStream,
      remoteStream,
      signalingUp,
      supported: env !== null,
      notice,
      startCall,
      accept,
      decline,
      hangup,
      setMuted,
      setCameraOff,
      hasVideo,
    }),
    [view, localStream, remoteStream, signalingUp, env, notice, startCall, accept, decline, hangup, setMuted, setCameraOff, hasVideo],
  )

  return <CallsContext.Provider value={value}>{children}</CallsContext.Provider>
}
