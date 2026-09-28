/**
 * AUTH-006: before email confirmation, `START_CONVERSATION` alone is denied.
 *
 * The banner used to say "to send messages and add contacts" — wrong by the
 * domain model (`_UNVERIFIED = {READ, SEND_MESSAGE}` in
 * `messenger/domain/user.py`): an unverified person can still read and reply
 * in a conversation that already exists. The gate is narrower — starting a
 * conversation with someone new — and the banner now says that, not more.
 * `ChatPage` renders this without touching `MessageComposer` at all: the
 * negative case ("unverified, existing conversation, still able to send") is
 * proven by the composer having no `emailVerified` branch to regress.
 */
import { useEffect, useRef, useState } from "react"
import { ApiProblem } from "../../../api/problems"
import { Icon } from "../../../shared/ui/Icon"

/** `POST /auth/verify-email/resend` — an operation assembled in `main.tsx`, same reason as `SearchUser`. */
export type ResendVerificationEmail = () => Promise<void>

/**
 * One value, not flags: `sending` and `rate-limited` together isn't a state
 * that exists, and two independent booleans would let the markup show both
 * at once depending on check order (same argument as `DialogState` in
 * `NewConversationDialog.tsx`).
 */
type ResendState =
  | { readonly kind: "idle" }
  | { readonly kind: "sending" }
  | { readonly kind: "sent" }
  | { readonly kind: "already-verified" }
  /** Absent `retryAfterSeconds` is a real case: the header is optional. */
  | { readonly kind: "rate-limited"; readonly retryAfterSeconds?: number }
  | { readonly kind: "failed" }

/**
 * `409`/`429` read by code and status, not by message text — `Problem.title`
 * is prose that changes independently of meaning (same rule as
 * `lookupFailure` in `NewConversationDialog.tsx`). Anything else — `503`,
 * a network failure, an unexpected `4xx` — collapses to `failed`: all three
 * are "didn't happen, try again", and the caller doesn't get to tell them
 * apart from an error instance alone (`ServiceUnavailableError` carries no
 * further code).
 */
function resendFailure(error: unknown): ResendState {
  if (error instanceof ApiProblem) {
    if (error.status === 409 && error.code === "already_verified") {
      return { kind: "already-verified" }
    }
    if (error.status === 429) {
      return { kind: "rate-limited", retryAfterSeconds: error.retryAfterSeconds }
    }
  }
  return { kind: "failed" }
}

function statusText(state: ResendState): string {
  switch (state.kind) {
    case "idle":
      return ""
    case "sending":
      return "Sending…"
    case "sent":
      return "Verification email sent."
    case "already-verified":
      return "This address is already verified. Refresh the page."
    case "rate-limited":
      return state.retryAfterSeconds === undefined
        ? "Too many attempts. Try again later."
        : `Too many attempts. Try again in ${state.retryAfterSeconds} seconds.`
    case "failed":
      return "Could not send the email. Try again."
  }
}

interface EmailVerificationBannerProps {
  readonly email: string
  readonly resend: ResendVerificationEmail
  /**
   * Wait after a `202` with no server-given number to read instead — `429`
   * uses `error.retryAfterSeconds` when the header is there. Configurable for
   * stories/tests, not for callers to tune in production: the default is the
   * one real value.
   */
  readonly defaultCooldownSeconds?: number
}

export function EmailVerificationBanner({
  email,
  resend,
  defaultCooldownSeconds = 30,
}: EmailVerificationBannerProps) {
  const [state, setState] = useState<ResendState>({ kind: "idle" })
  const [secondsLeft, setSecondsLeft] = useState(0)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) clearInterval(timerRef.current)
    }
  }, [])

  function startCooldown(seconds: number): void {
    if (timerRef.current !== null) clearInterval(timerRef.current)
    setSecondsLeft(seconds)
    if (seconds <= 0) return
    timerRef.current = setInterval(() => {
      setSecondsLeft((s) => {
        if (s <= 1) {
          if (timerRef.current !== null) clearInterval(timerRef.current)
          timerRef.current = null
          return 0
        }
        return s - 1
      })
    }, 1000)
  }

  async function handleResend(): Promise<void> {
    if (state.kind === "sending" || secondsLeft > 0) return
    setState({ kind: "sending" })
    try {
      await resend()
      setState({ kind: "sent" })
      startCooldown(defaultCooldownSeconds)
    } catch (error) {
      const failure = resendFailure(error)
      setState(failure)
      // `already-verified`/`failed` force no wait: the first asks for a
      // reload, not a retry, and the second is exactly the case the person
      // should be free to try again without a manufactured delay.
      if (failure.kind === "rate-limited") {
        startCooldown(failure.retryAfterSeconds ?? defaultCooldownSeconds)
      }
    }
  }

  const busy = state.kind === "sending" || secondsLeft > 0

  return (
    <div
      data-verification-banner-state={state.kind}
      className="flex flex-shrink-0 flex-wrap items-center justify-center gap-x-3 gap-y-1 bg-accent-amber/15 px-4 py-2.5 text-sm text-status-warning"
    >
      <Icon name="mark_email_unread" size={18} />
      <span>
        Confirm <strong className="font-semibold">{email}</strong> to start new conversations.
      </span>
      <button
        type="button"
        data-verification-resend
        onClick={() => void handleResend()}
        disabled={busy}
        className="font-semibold text-accent-terracotta hover:underline disabled:cursor-not-allowed disabled:text-text-warm-muted disabled:no-underline"
      >
        {secondsLeft > 0 ? `Resend in ${secondsLeft}s` : "Resend email"}
      </button>
      {/*
        Empty and rendered on `idle` — not omitted — so the line height stays
        put: an element that appears only on the first result would shift the
        banner down by one line the moment a person clicks resend.
      */}
      <span data-verification-status role="status" className="text-xs text-text-warm-secondary">
        {statusText(state)}
      </span>
    </div>
  )
}
