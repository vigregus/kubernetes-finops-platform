import { describe, expect, it } from "vitest"

import type { ConnectionMachineState } from "../realtime/connectionMachine"
import {
  INITIAL_CONNECTION_TELEMETRY_CONTEXT,
  mapConnectionEvent,
} from "./connectionTelemetry"

const NOW = () => new Date("2026-09-19T00:00:00Z")

function состояние(overrides: Partial<ConnectionMachineState> = {}): ConnectionMachineState {
  return { state: "connecting", syncReason: null, browserOnline: true, reconnectAllowed: true, ...overrides }
}

function типы(mapped: { events: readonly { type: string }[] }): string[] {
  return mapped.events.map((e) => e.type)
}

describe("mapConnectionEvent", () => {
  it("первый sdk-connected — ws_connected, второй после разрыва — ws_reconnected", () => {
    const first = mapConnectionEvent(
      { type: "sdk-connected" },
      состояние({ state: "connecting" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(first)).toEqual(["ws_connected"])
    expect(first.context.everConnected).toBe(true)

    const second = mapConnectionEvent(
      { type: "sdk-connected" },
      состояние({ state: "connecting" }),
      first.context,
      NOW,
    )
    expect(типы(second)).toEqual(["ws_reconnected"])
  })

  it("sdk-connected не считается, пока автомат ещё синхронизируется", () => {
    const mapped = mapConnectionEvent(
      { type: "sdk-connected" },
      состояние({ state: "syncing", syncReason: "sequence-gap" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual([])
  })

  it("sdk-connected не считается при офлайне браузера", () => {
    const mapped = mapConnectionEvent(
      { type: "sdk-connected" },
      состояние({ browserOnline: false }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual([])
  })

  it("browser-offline и sdk-disconnected дают ws_disconnected ровно один раз на разрыв", () => {
    const first = mapConnectionEvent(
      { type: "browser-offline" },
      состояние({ state: "connected" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(first)).toEqual(["ws_disconnected"])

    // Второй сигнал того же разрыва (код сокета вслед за офлайном браузера)
    // застаёт состояние уже "disconnected" - и не задваивает событие.
    const second = mapConnectionEvent(
      { type: "sdk-disconnected", code: 1006 },
      состояние({ state: "disconnected" }),
      first.context,
      NOW,
    )
    expect(типы(second)).toEqual([])
  })

  it("subscription-subscribed с восстановлением — recovery_success, без sync", () => {
    const mapped = mapConnectionEvent(
      { type: "subscription-subscribed", wasRecovering: true, recovered: true },
      состояние({ state: "connecting" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual(["recovery_success"])
  })

  it("subscription-subscribed без восстановления — recovery_failed и sync_started", () => {
    const mapped = mapConnectionEvent(
      { type: "subscription-subscribed", wasRecovering: true, recovered: false },
      состояние({ state: "connecting" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual(["recovery_failed", "sync_started"])
  })

  it("первая подписка (wasRecovering=false) не считается восстановлением", () => {
    const mapped = mapConnectionEvent(
      { type: "subscription-subscribed", wasRecovering: false, recovered: false },
      состояние({ state: "connecting" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual([])
  })

  it("sequence-gap при онлайне — gap_detected и sync_started", () => {
    const mapped = mapConnectionEvent(
      { type: "sequence-gap" },
      состояние({ state: "connected" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual(["gap_detected", "sync_started"])
  })

  it("sequence-gap при офлайне ничего не даёт — тем же условием, что у transition()", () => {
    const mapped = mapConnectionEvent(
      { type: "sequence-gap" },
      состояние({ browserOnline: false }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual([])
  })

  it("sync-completed из syncing — sync_finished", () => {
    const mapped = mapConnectionEvent(
      { type: "sync-completed" },
      состояние({ state: "syncing", syncReason: "sequence-gap" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual(["sync_finished"])
  })

  it("sync-completed вне syncing не даёт события — переходу не из чего выйти", () => {
    const mapped = mapConnectionEvent(
      { type: "sync-completed" },
      состояние({ state: "connected" }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual([])
  })

  it("browser-online сам по себе не сообщает ничего", () => {
    const mapped = mapConnectionEvent(
      { type: "browser-online" },
      состояние({ state: "disconnected", browserOnline: false }),
      INITIAL_CONNECTION_TELEMETRY_CONTEXT,
      NOW,
    )
    expect(типы(mapped)).toEqual([])
  })
})
