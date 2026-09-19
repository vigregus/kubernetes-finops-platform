import { useState } from "react"
import { Icon } from "../atoms/Icon"

interface EmailVerificationBannerProps {
  email: string
  onResend: () => void
  resendCooldownSeconds?: number
}

/** AUTH-006: before email confirmation, functionality is limited; resend is rate-limited. */
export function EmailVerificationBanner({ email, onResend, resendCooldownSeconds = 30 }: EmailVerificationBannerProps) {
  const [cooldown, setCooldown] = useState(0)

  function handleResend() {
    onResend()
    setCooldown(resendCooldownSeconds)
    const timer = setInterval(() => {
      setCooldown((s) => {
        if (s <= 1) {
          clearInterval(timer)
          return 0
        }
        return s - 1
      })
    }, 1000)
  }

  return (
    <div className="flex flex-shrink-0 items-center justify-center gap-3 bg-accent-amber/15 px-4 py-2.5 text-sm text-status-warning">
      <Icon name="mark_email_unread" size={18} />
      <span>
        Confirm <strong className="font-semibold">{email}</strong> to send messages and add contacts.
      </span>
      <button
        type="button"
        onClick={handleResend}
        disabled={cooldown > 0}
        className="font-semibold text-accent-terracotta hover:underline disabled:cursor-not-allowed disabled:text-text-warm-muted disabled:no-underline"
      >
        {cooldown > 0 ? `Resend in ${cooldown}s` : "Resend email"}
      </button>
    </div>
  )
}
