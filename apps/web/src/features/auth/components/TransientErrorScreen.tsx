import { Icon } from "../../../shared/ui/Icon"

interface TransientErrorScreenProps {
  /**
   * Идентификатор трассы из `Problem`, если сервер его прислал.
   *
   * Показывается, а не прячется: это единственное, что человек может назвать,
   * когда сервис недоступен, и единственное, чем его отказ отличается от
   * «что-то пошло не так».
   */
  traceId?: string
  onRetry: () => void
}

/**
 * «Сервис недоступен» — отдельное состояние, а не разновидность отказа входа.
 *
 * Сервер разделяет их намеренно: `503` означает «повтори позже», `401` —
 * «войди заново», и слить их значило бы «отправлять человека на повторный вход
 * в момент, когда вход всё равно не работает» (`_login_failure`,
 * `api/main.py:307-317`). Поэтому здесь нет ни слова об истёкшей сессии, ни
 * кнопки входа: единственное действие — повтор, и оно честное, потому что
 * следующий запрос вполне может пройти.
 *
 * Cookie сюда не относится: её снимает сервер при **любом** неудачном обмене,
 * включая `503` (`main.py:377`), и повтор после отказа может законно получить
 * `401`. Это серверная семантика, принятая явно, а не обойдённая.
 */
export function TransientErrorScreen({ traceId, onRetry }: TransientErrorScreenProps) {
  return (
    <div className="flex h-dvh w-full flex-col items-center justify-center gap-5 bg-surface px-6 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface-cream text-text-warm-muted shadow-[0_1px_4px_rgba(41,37,36,0.05)]">
        <Icon name="cloud_off" size={26} />
      </div>
      <div>
        <h1 className="text-lg font-semibold tracking-tight text-on-surface">Service unavailable</h1>
        <p className="mt-1 text-sm text-text-warm-secondary">We couldn&apos;t reach the server. Please try again.</p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="flex items-center gap-2 rounded-xl bg-accent-terracotta px-6 py-3 text-sm font-semibold text-on-primary shadow-[0_2px_8px_rgba(234,88,12,0.25)] transition-all hover:bg-status-error active:scale-95"
      >
        Try again
        <Icon name="refresh" size={18} />
      </button>
      {traceId && <p className="text-xs text-text-warm-muted">Reference: {traceId}</p>}
    </div>
  )
}
