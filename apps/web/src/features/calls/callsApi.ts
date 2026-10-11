/**
 * Операции звонков в форме, нужной клиенту. Сборка из сгенерированного `CallsApi`
 * — в `main.tsx` (там живёт клиент API), здесь — только форма и перевод модели.
 */
import type { Call, CallSignal, CallSignalEvent } from "../../api/generated"
import type { CallProfile, ConnectionDetail, IceConfig, WireSignal } from "./callEngine"
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
  /**
   * Соединение не состоялось или оборвалось насовсем: итог `failed`, а не `completed`.
   * `reason` — причина по таксономии отказов (метрика `RES-009`); необязательна.
   */
  fail(callId: string, reason?: string): Promise<ApiCallView>
  keepalive(callId: string): Promise<ApiCallView>
  connected(
    callId: string,
    connectionType: "direct" | "relay",
    detail?: ConnectionDetail,
  ): Promise<ApiCallView>
  signal(callId: string, signal: WireSignal): Promise<void>
  /**
   * Пропущенные сигналы собеседника с номером больше `after`, по порядку. Каждый —
   * в той же форме, что публикация канала (`call.signal`): их применяет тот же
   * обработчик. Канал звонков без истории, поэтому после обрыва соединения это
   * единственный путь вернуть потерянный `offer`/`answer`.
   */
  signals(callId: string, after: number): Promise<readonly unknown[]>
  /** Данные STUN/TURN и срок их действия (минуты): клиент обновляет их до конца срока. */
  iceServers(callId: string, profile?: CallProfile): Promise<IceConfig>
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
  const id = signal.signalId === undefined ? {} : { signalId: signal.signalId }
  if (signal.type === "ice") {
    return {
      ...id,
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
  return { ...id, type: signal.type, sdp: signal.sdp }
}

/** Сигнал из выдачи пропущенных → форма публикации канала (`call.signal`). */
export function toSignalPayload(event: CallSignalEvent): Record<string, unknown> {
  return {
    type: "call.signal",
    call_id: event.callId,
    seq: event.seq,
    ...(event.signalId === undefined ? {} : { signal_id: event.signalId }),
    signal: {
      type: event.signal.type,
      ...(event.signal.sdp === undefined ? {} : { sdp: event.signal.sdp }),
      ...(event.signal.candidates === undefined ? {} : { candidates: event.signal.candidates }),
    },
  }
}
