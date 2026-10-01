/**
 * Двенадцать событий из контракта (`06-observability.md`, "Телеметрия
 * браузера обязательна" / `openapi.yaml`, `BrowserTelemetryEvent`), одним
 * доменным типом, а не сгенерированным DTO на месте вызова: хук, который
 * репортит `message_rendered`, не обязан знать про camelCase/snake_case
 * транспорта - этим занимается `telemetryClient.ts`, единственное место,
 * которое видит сгенерированный `BrowserTelemetryEvent`.
 */
export type TelemetryEventType =
  | "ws_connected"
  | "ws_disconnected"
  | "ws_reconnected"
  | "gap_detected"
  | "message_received"
  | "message_rendered"
  | "delivery_ack"
  | "recovery_success"
  | "recovery_failed"
  | "sync_started"
  | "sync_finished"
  | "js_error"

/**
 * `messageId` обязателен ровно у трёх типов на транспорте
 * (`message_received`/`message_rendered`/`delivery_ack` - контракт
 * отвергает его отсутствие 422-м), но здесь остаётся необязательным
 * полем одного общего типа, а не отдельным вариантом объединения: цена
 * второго была бы в сложности вызывающего кода ради проверки, которую
 * и так делает сервер.
 */
export interface TelemetryEvent {
  readonly type: TelemetryEventType
  readonly occurredAt: Date
  readonly messageId?: string
  readonly conversationId?: string
  readonly detail?: string
}
