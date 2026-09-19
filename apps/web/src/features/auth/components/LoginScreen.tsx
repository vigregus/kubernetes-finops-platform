import { Icon } from "../../../shared/ui/Icon"

interface LoginScreenProps {
  /** AUTH-004: token refresh failed — client must prompt re-login, not fail silently. */
  sessionExpired?: boolean
  onLogin: () => void
}

export function LoginScreen({ sessionExpired, onLogin }: LoginScreenProps) {
  return (
    <div className="flex h-dvh w-full flex-col items-center justify-center gap-6 bg-surface px-6 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-terracotta text-on-primary shadow-sm">
        <Icon name="forum" size={28} />
      </div>
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-on-surface">Vector</h1>
        <p className="mt-1 text-sm text-text-warm-secondary">Encrypted messaging, without the noise.</p>
      </div>

      {sessionExpired && (
        <div className="flex items-center gap-2 rounded-xl bg-error-container px-4 py-2.5 text-sm text-status-error">
          <Icon name="lock_clock" size={18} />
          Your session expired. Sign in again to continue.
        </div>
      )}

      <button
        type="button"
        onClick={onLogin}
        className="flex items-center gap-2 rounded-xl bg-accent-terracotta px-6 py-3 text-sm font-semibold text-on-primary shadow-[0_2px_8px_rgba(234,88,12,0.25)] transition-all hover:bg-status-error active:scale-95"
      >
        Continue with Vector ID
        <Icon name="arrow_forward" size={18} />
      </button>
    </div>
  )
}
