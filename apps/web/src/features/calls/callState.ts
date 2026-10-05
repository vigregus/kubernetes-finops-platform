/**
 * Автомат звонка на клиенте — чистые правила без WebRTC и сети.
 *
 * Правду о звонке хранит **сервер** (`domain/call.py`): клиент не решает, жив ли
 * звонок, а повторяет то, что ему сказали события и ответы. Поэтому здесь нет
 * таймеров и догадок — только три защиты, ради которых автомат и вынесен:
 *
 *  - **номер состояния** (`version`): событие с номером не больше виденного
 *    отбрасывается, и запоздавшее `ringing` не оживит завершённый звонок;
 *  - **номер сигнала** (`seq`, выдаёт сервер): устаревший `offer` из прошлого
 *    соединения не применяется (`CALL-007`);
 *  - **один звонок на вкладку**: второй входящий при идущем звонке игнорируется —
 *    «занято» решает сервер, а клиент не заводит второй экран.
 */

export type CallKind = "audio" | "video"
export type CallRole = "caller" | "callee"

/** Что видит человек. `connecting` — принят, медиа ещё не пошло. */
export type CallPhase = "idle" | "outgoing" | "incoming" | "connecting" | "active" | "ended"

/** Причины конца, как их называет сервер, плюс клиентские. */
export type CallEndReason =
  | "completed"
  | "declined"
  | "missed"
  | "cancelled"
  | "busy"
  | "unavailable"
  | "failed"
  | "accepted_elsewhere"
  | "media_denied"

export interface CallView {
  readonly phase: CallPhase
  readonly callId: string | null
  readonly conversationId: string | null
  readonly kind: CallKind
  readonly role: CallRole | null
  readonly peerName: string | null
  /** Последний принятый номер состояния; `0` — звонка ещё не было. */
  readonly version: number
  /** Последний применённый номер сигнала. */
  readonly lastSignalSeq: number
  readonly endReason: CallEndReason | null
  readonly muted: boolean
  readonly cameraOff: boolean
}

export const IDLE: CallView = {
  phase: "idle",
  callId: null,
  conversationId: null,
  kind: "audio",
  role: null,
  peerName: null,
  version: 0,
  lastSignalSeq: 0,
  endReason: null,
  muted: false,
  cameraOff: false,
}

/** Звонок в форме, как его отдаёт API (`Call` контракта, без лишнего). */
export interface ApiCallView {
  readonly callId: string
  readonly conversationId: string
  readonly kind: CallKind
  readonly state: "ringing" | "accepted" | "active" | "ended"
  readonly endReason: string | null
  readonly version: number
  readonly role: CallRole
}

export type CallAction =
  /** Ответ API или `current`: звонок уже существует на сервере. */
  | { readonly type: "api-call"; readonly call: ApiCallView; readonly peerName: string | null }
  /** Событие `call.incoming`. */
  | {
      readonly type: "incoming"
      readonly callId: string
      readonly conversationId: string
      readonly kind: CallKind
      readonly peerName: string
      readonly version: number
    }
  /** Событие `call.state`. */
  | {
      readonly type: "state"
      readonly callId: string
      readonly state: "ringing" | "accepted" | "active" | "ended"
      readonly reason: string | null
      readonly version: number
    }
  /** Человек принял входящий — экран сразу в «соединяется», не дожидаясь ответа. */
  | { readonly type: "local-accepting" }
  | { readonly type: "local-ended"; readonly reason: CallEndReason }
  | { readonly type: "connected" }
  | { readonly type: "signal-applied"; readonly seq: number }
  | { readonly type: "set-muted"; readonly muted: boolean }
  | { readonly type: "set-camera-off"; readonly cameraOff: boolean }
  | { readonly type: "reset" }

const KNOWN_REASONS: readonly string[] = [
  "completed",
  "declined",
  "missed",
  "cancelled",
  "busy",
  "unavailable",
  "failed",
  "accepted_elsewhere",
]

function reasonOf(value: string | null): CallEndReason {
  return value !== null && KNOWN_REASONS.includes(value) ? (value as CallEndReason) : "failed"
}

export function isLive(view: CallView): boolean {
  return view.phase !== "idle" && view.phase !== "ended"
}

function fromApi(call: ApiCallView, peerName: string | null): CallView {
  const base = {
    ...IDLE,
    callId: call.callId,
    conversationId: call.conversationId,
    kind: call.kind,
    role: call.role,
    peerName,
    version: call.version,
  }
  switch (call.state) {
    case "ringing":
      return { ...base, phase: call.role === "caller" ? "outgoing" : "incoming" }
    case "accepted":
      return { ...base, phase: "connecting" }
    case "active":
      return { ...base, phase: "active" }
    case "ended":
      return { ...base, phase: "ended", endReason: reasonOf(call.endReason) }
  }
}

export function callReducer(view: CallView, action: CallAction): CallView {
  switch (action.type) {
    case "api-call": {
      // Ответ на «начать» не должен затереть уже идущий звонок этой вкладки.
      if (isLive(view) && view.callId !== action.call.callId) return view
      if (view.callId === action.call.callId) {
        // Тот же звонок: ответ API и событие приходят в любом порядке, и
        // устаревшее отбрасывается по номеру. Применённые сигналы и выключенные
        // микрофон с камерой при этом сохраняются: обнулённый `lastSignalSeq`
        // позволил бы применить тот же `offer` второй раз.
        if (action.call.version <= view.version) return view
        return {
          ...fromApi(action.call, action.peerName ?? view.peerName),
          lastSignalSeq: view.lastSignalSeq,
          muted: view.muted,
          cameraOff: view.cameraOff,
        }
      }
      return fromApi(action.call, action.peerName ?? view.peerName)
    }

    case "incoming": {
      if (isLive(view)) return view
      return {
        ...IDLE,
        phase: "incoming",
        callId: action.callId,
        conversationId: action.conversationId,
        kind: action.kind,
        role: "callee",
        peerName: action.peerName,
        version: action.version,
      }
    }

    case "state": {
      if (view.callId !== action.callId) return view
      // Устаревшее — молча: клиент, отставший на один номер, не должен «ожить».
      if (action.version <= view.version) return view
      const next = { ...view, version: action.version }
      if (action.reason === "accepted_elsewhere") {
        // Вызов принят в другой вкладке этого человека: экран гаснет, но
        // звонок не закончился. В принявшей вкладке событие игнорируется —
        // она сама уже в `connecting`.
        return view.phase === "incoming" ? { ...next, phase: "ended", endReason: "accepted_elsewhere" } : next
      }
      switch (action.state) {
        case "ringing":
          return next
        case "accepted":
          return view.phase === "ended" ? view : { ...next, phase: view.phase === "active" ? "active" : "connecting" }
        case "active":
          return view.phase === "ended" ? view : { ...next, phase: "active" }
        case "ended":
          return { ...next, phase: "ended", endReason: reasonOf(action.reason) }
      }
      return next
    }

    case "local-accepting":
      return view.phase === "incoming" ? { ...view, phase: "connecting" } : view

    case "local-ended":
      return isLive(view) ? { ...view, phase: "ended", endReason: action.reason } : view

    case "connected":
      return view.phase === "connecting" ? { ...view, phase: "active" } : view

    case "signal-applied":
      return action.seq > view.lastSignalSeq ? { ...view, lastSignalSeq: action.seq } : view

    case "set-muted":
      return { ...view, muted: action.muted }

    case "set-camera-off":
      return { ...view, cameraOff: action.cameraOff }

    case "reset":
      return IDLE
  }
}

/** Применять ли сигнал: только текущего звонка и только новее виденного. */
export function shouldApplySignal(view: CallView, callId: string, seq: number): boolean {
  return isLive(view) && view.callId === callId && seq > view.lastSignalSeq
}
