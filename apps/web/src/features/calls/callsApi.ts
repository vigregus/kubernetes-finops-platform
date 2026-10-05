/**
 * Операции звонков в форме, нужной клиенту. Сборка из сгенерированного `CallsApi`
 * — в `main.tsx` (там живёт клиент API), здесь — только форма и перевод модели.
 */
import type { Call, CallSignal } from "../../api/generated"
import type { WireSignal } from "./callEngine"
import type { ApiCallView } from "./callState"

export interface CallsOperations {
  start(conversationId: string, kind: "audio" | "video"): Promise<ApiCallView>
  /** Мой живой звонок или `null`. */
  current(): Promise<ApiCallView | null>
  accept(callId: string): Promise<ApiCallView>
  decline(callId: string): Promise<ApiCallView>
  hangup(callId: string): Promise<ApiCallView>
  keepalive(callId: string): Promise<ApiCallView>
  connected(callId: string, connectionType: "direct" | "relay"): Promise<ApiCallView>
  signal(callId: string, signal: WireSignal): Promise<void>
  iceServers(callId: string): Promise<RTCIceServer[]>
}

/** Модель API → то, что знает автомат клиента. */
export function toCallView(call: Call): ApiCallView {
  return {
    callId: call.callId,
    conversationId: call.conversationId,
    kind: call.kind,
    state: call.state,
    endReason: call.endReason ?? null,
    version: call.version,
    role: call.role,
  }
}

/** Сигнал клиента → тело запроса контракта. */
export function toCallSignal(signal: WireSignal): CallSignal {
  if (signal.type === "ice") {
    return {
      type: "ice",
      candidates: signal.candidates.map((candidate) => ({
        candidate: candidate.candidate ?? "",
        // Отсутствующее поле остаётся отсутствующим, а не `null`: сервер
        // принимает только строку и число.
        ...(typeof candidate.sdpMid === "string" ? { sdpMid: candidate.sdpMid } : {}),
        ...(typeof candidate.sdpMLineIndex === "number"
          ? { sdpMLineIndex: candidate.sdpMLineIndex }
          : {}),
      })),
    }
  }
  return { type: signal.type, sdp: signal.sdp }
}
