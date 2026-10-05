/**
 * Операции звонков в форме, нужной клиенту. Сборка из сгенерированного `CallsApi`
 * — в `main.tsx` (там живёт клиент API), здесь — только форма и перевод модели.
 */
import type { Call, CallSignal } from "../../api/generated"
import type { IceConfig, WireSignal } from "./callEngine"
import type { ApiCallView } from "./callState"

export interface CallsOperations {
  start(conversationId: string, kind: "audio" | "video"): Promise<ApiCallView>
  /** Мой живой звонок или `null`. */
  current(): Promise<ApiCallView | null>
  /** `tabId` — метка этой вкладки: проигравшая гонку «принять» получает `call_taken`. */
  accept(callId: string, tabId: string): Promise<ApiCallView>
  decline(callId: string): Promise<ApiCallView>
  /** `unload` — страница закрывается: запрос должен пережить её (`keepalive`). */
  hangup(callId: string, options?: { readonly unload?: boolean }): Promise<ApiCallView>
  /** Соединение не состоялось или оборвалось насовсем: итог `failed`, а не `completed`. */
  fail(callId: string): Promise<ApiCallView>
  keepalive(callId: string): Promise<ApiCallView>
  connected(callId: string, connectionType: "direct" | "relay"): Promise<ApiCallView>
  signal(callId: string, signal: WireSignal): Promise<void>
  /** Данные STUN/TURN и срок их действия (минуты): клиент обновляет их до конца срока. */
  iceServers(callId: string): Promise<IceConfig>
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
