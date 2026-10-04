import type { PushState } from "./pushClient"

interface NotificationPromptProps {
  state: PushState
  onTurnOn: () => void
}

/**
 * Предложение включить уведомления.
 *
 * Показывается **только когда есть что предложить**: человек не запрещал, а
 * браузер умеет. Запрет в браузере (`NTF-005`) называется один раз и
 * объясняет, где это поменять; приложение при нём работает как прежде.
 * Остальные состояния — тишина: «не поддерживается» и «сервер не настроил» —
 * не повод тревожить человека.
 */
export function NotificationPrompt({ state, onTurnOn }: NotificationPromptProps) {
  if (state.kind === "off" || state.kind === "working") {
    return (
      <div
        data-notifications-state={state.kind}
        className="flex items-center justify-between gap-3 bg-surface-container-low px-4 py-2 text-sm text-text-charcoal"
      >
        <span>Get notified about new messages when this tab is closed.</span>
        <button
          type="button"
          data-notifications-enable
          disabled={state.kind === "working"}
          onClick={onTurnOn}
          className="rounded-lg bg-accent-terracotta px-3 py-1 text-xs font-semibold text-on-primary disabled:opacity-60"
        >
          {state.kind === "working" ? "Turning on…" : "Turn on"}
        </button>
      </div>
    )
  }
  if (state.kind === "denied") {
    return (
      <div
        data-notifications-state="denied"
        className="bg-surface-container-low px-4 py-2 text-xs text-text-warm-muted"
      >
        Notifications are blocked in this browser. Allow them in the site settings to get alerts.
      </div>
    )
  }
  if (state.kind === "error") {
    return (
      <div
        data-notifications-state="error"
        className="flex items-center justify-between gap-3 bg-error-container/40 px-4 py-2 text-xs text-status-error"
      >
        <span>{state.message}</span>
        <button type="button" data-notifications-enable onClick={onTurnOn} className="font-semibold underline">
          Try again
        </button>
      </div>
    )
  }
  return null
}
