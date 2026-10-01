/**
 * Факт автомата соединения (`features/realtime/connectionMachine.ts`) →
 * события телеметрии G3-008. Чистая функция, тем же доводом, что у самого
 * автомата: переходы доказываются прогоном без SDK и без React.
 *
 * Условия внутри каждой ветки **повторяют** условия соответствующей ветки
 * `transition()`, а не изобретают свои: тождество «телеметрия видит ровно
 * то же решение, что принял автомат» иначе нечем доказать — разошедшееся
 * условие тихо считало бы не то же событие, что реально сменило состояние
 * интерфейса.
 *
 * `ws_connected` и `ws_reconnected` эмитятся только из `sdk-connected`, не
 * из `subscription-subscribed`: оба события в реальном подключении обычно
 * идут подряд (сокет, потом подписка канала), и считать по обоим значило
 * бы задвоить факт. `subscription-subscribed` отвечает только за исход
 * восстановления канала (`recovery_success`/`recovery_failed`) — другой,
 * не пересекающийся вопрос.
 */
import type { ConnectionEvent, ConnectionMachineState } from "../realtime/connectionMachine";
import type { TelemetryEvent } from "./types";

export interface ConnectionTelemetryContext {
  /** Был ли уже хоть один успешный `sdk-connected` за время жизни хука. */
  readonly everConnected: boolean;
}

export const INITIAL_CONNECTION_TELEMETRY_CONTEXT: ConnectionTelemetryContext = {
  everConnected: false,
};

export interface MappedConnectionTelemetry {
  readonly events: readonly TelemetryEvent[];
  readonly context: ConnectionTelemetryContext;
}

export function mapConnectionEvent(
  event: ConnectionEvent,
  before: ConnectionMachineState,
  context: ConnectionTelemetryContext,
  now: () => Date = () => new Date(),
): MappedConnectionTelemetry {
  const events: TelemetryEvent[] = [];
  let everConnected = context.everConnected;

  switch (event.type) {
    case "browser-offline":
    case "sdk-disconnected":
      // Не задваивается на повторном «уже разорвано»: несколько сигналов
      // одного и того же разрыва (офлайн браузера и следом код сокета)
      // не должны выглядеть как два разных обрыва связи.
      if (before.state !== "disconnected") {
        events.push({ type: "ws_disconnected", occurredAt: now() });
      }
      break;

    case "sdk-connected":
      // То же условие, что у `transition()`: `browserOnline && state !== "syncing"`.
      if (before.browserOnline && before.state !== "syncing") {
        events.push({ type: everConnected ? "ws_reconnected" : "ws_connected", occurredAt: now() });
        everConnected = true;
      }
      break;

    case "subscription-subscribed":
      if (event.wasRecovering && !event.recovered) {
        // Тот же исход, что `syncing(state, "recovery-miss")` у автомата:
        // SDK не смог восстановить канал сам, и это и есть «доля
        // recovered=false» из `PERF-005`.
        events.push({ type: "recovery_failed", occurredAt: now() });
        events.push({ type: "sync_started", occurredAt: now() });
      } else if (event.wasRecovering) {
        events.push({ type: "recovery_success", occurredAt: now() });
      }
      break;

    case "sequence-gap":
      if (before.browserOnline) {
        events.push({ type: "gap_detected", occurredAt: now() });
        events.push({ type: "sync_started", occurredAt: now() });
      }
      break;

    case "sync-completed":
      if (before.state === "syncing" && before.browserOnline) {
        events.push({ type: "sync_finished", occurredAt: now() });
      }
      break;

    case "browser-online":
      // Само по себе ничего не сообщает: оно лишь разрешает автомату
      // попытаться снова (`state: "connecting"`), а успех или его
      // отсутствие телеметрия увидит позже - через `sdk-connected`,
      // `sdk-disconnected` или `subscription-subscribed`.
      break;
  }

  return { events, context: { everConnected } };
}
