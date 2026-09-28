import { act, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ApiProblem, ServiceUnavailableError } from "../../../api/problems"
import { EmailVerificationBanner } from "./EmailVerificationBanner"

/**
 * `202`/`409`/`429`/`503` — по коду и статусу, не по тексту (`resendFailure`
 * в модуле). Обёртки живой отказ уже приводит к этим же классам
 * (`api/client.ts::check`), так что здесь достаточно бросить их напрямую —
 * без сети и без `withUnwrappedErrors`.
 */

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

function renderBanner(resend: () => Promise<void>, defaultCooldownSeconds?: number) {
  return render(
    <EmailVerificationBanner
      email="david.miller@example.com"
      resend={resend}
      defaultCooldownSeconds={defaultCooldownSeconds}
    />,
  )
}

it("называет причину, ради которой почта нужна, узко — не «отправку сообщений»", () => {
  renderBanner(() => new Promise(() => {}))
  expect(screen.getByText(/start new conversations/i)).toBeTruthy()
  expect(screen.queryByText(/send messages/i)).toBeNull()
})

describe("202: письмо отправлено", () => {
  it("показывает исход и включает паузу по умолчанию", async () => {
    let resolveSend: () => void = () => {}
    const resend = () =>
      new Promise<void>((resolve) => {
        resolveSend = resolve
      })
    renderBanner(resend, 30)

    fireEvent.click(screen.getByText("Resend email"))
    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(true)

    await act(async () => {
      resolveSend()
      await Promise.resolve()
    })

    expect(screen.getByText("Verification email sent.")).toBeTruthy()
    expect(screen.getByText("Resend in 30s")).toBeTruthy()

    act(() => {
      vi.advanceTimersByTime(30_000)
    })
    expect(screen.getByText("Resend email")).toBeTruthy()
    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(false)
  })
})

describe("429: сервер попросил подождать", () => {
  it("считает от Retry-After сервера, а не от локальных 30 секунд", async () => {
    const resend = () =>
      Promise.reject(new ApiProblem({ status: 429, code: "rate_limited", retryAfterSeconds: 5 }))
    renderBanner(resend, 30)

    await act(async () => {
      fireEvent.click(screen.getByText("Resend email"))
      await Promise.resolve()
    })

    // Пять секунд сервера, а не тридцать по умолчанию — здесь и есть предмет
    // проверки: заголовок обязан победить локальное число.
    expect(screen.getByText("Resend in 5s")).toBeTruthy()

    act(() => {
      vi.advanceTimersByTime(5_000)
    })
    expect(screen.getByText("Resend email")).toBeTruthy()
  })

  it("без Retry-After откатывается на локальную паузу, а не оставляет кнопку живой", async () => {
    const resend = () => Promise.reject(new ApiProblem({ status: 429, code: "rate_limited" }))
    renderBanner(resend, 12)

    await act(async () => {
      fireEvent.click(screen.getByText("Resend email"))
      await Promise.resolve()
    })

    expect(screen.getByText("Too many attempts. Try again later.")).toBeTruthy()
    expect(screen.getByText("Resend in 12s")).toBeTruthy()
  })
})

describe("503 и сетевой отказ: recoverable, а не тишина", () => {
  it("503 предлагает попробовать снова, без вынужденной паузы", async () => {
    const resend = () => Promise.reject(new ServiceUnavailableError("Сервис недоступен"))
    renderBanner(resend, 30)

    await act(async () => {
      fireEvent.click(screen.getByText("Resend email"))
      await Promise.resolve()
    })

    expect(screen.getByText("Could not send the email. Try again.")).toBeTruthy()
    // Отказ инфраструктуры — не отказ по правилу: повторить можно сразу же.
    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(false)
  })

  it("сетевой отказ (без Response вовсе) — тот же исход, не падение компонента", async () => {
    const resend = () => Promise.reject(new TypeError("Failed to fetch"))
    renderBanner(resend, 30)

    await act(async () => {
      fireEvent.click(screen.getByText("Resend email"))
      await Promise.resolve()
    })

    expect(screen.getByText("Could not send the email. Try again.")).toBeTruthy()
  })
})

describe("409: адрес уже подтверждён", () => {
  it("предлагает обновить страницу, а не притворяется, что письмо ушло", async () => {
    const resend = () =>
      Promise.reject(new ApiProblem({ status: 409, code: "already_verified" }))
    renderBanner(resend, 30)

    await act(async () => {
      fireEvent.click(screen.getByText("Resend email"))
      await Promise.resolve()
    })

    expect(screen.getByText(/already verified/i)).toBeTruthy()
    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(false)
  })
})
