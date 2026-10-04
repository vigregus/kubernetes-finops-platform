import { cleanup, fireEvent, render } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { NotificationPrompt } from "./NotificationPrompt"

afterEach(cleanup)

describe("NotificationPrompt", () => {
  it("предлагает включить и вызывает включение по нажатию", () => {
    const onTurnOn = vi.fn()
    const { container } = render(<NotificationPrompt state={{ kind: "off" }} onTurnOn={onTurnOn} />)
    fireEvent.click(container.querySelector("[data-notifications-enable]") as HTMLButtonElement)
    expect(onTurnOn).toHaveBeenCalledTimes(1)
  })

  it("пока идёт включение, кнопка недоступна", () => {
    const { container } = render(<NotificationPrompt state={{ kind: "working" }} onTurnOn={vi.fn()} />)
    expect((container.querySelector("[data-notifications-enable]") as HTMLButtonElement).disabled).toBe(true)
  })

  it("запрет в браузере объясняется, но кнопки включения нет (NTF-005)", () => {
    const { container } = render(<NotificationPrompt state={{ kind: "denied" }} onTurnOn={vi.fn()} />)
    expect(container.querySelector("[data-notifications-state='denied']")).not.toBeNull()
    expect(container.querySelector("[data-notifications-enable]")).toBeNull()
  })

  it("ошибка называется и даёт повторить", () => {
    const onTurnOn = vi.fn()
    const { container, getByText } = render(
      <NotificationPrompt state={{ kind: "error", message: "Couldn't turn on" }} onTurnOn={onTurnOn} />,
    )
    expect(getByText("Couldn't turn on")).toBeTruthy()
    fireEvent.click(container.querySelector("[data-notifications-enable]") as HTMLButtonElement)
    expect(onTurnOn).toHaveBeenCalled()
  })

  it.each([{ kind: "on" }, { kind: "unsupported" }, { kind: "unavailable" }, { kind: "checking" }] as const)(
    "в состоянии %o ничего не рисует",
    (state) => {
      const { container } = render(<NotificationPrompt state={state} onTurnOn={vi.fn()} />)
      expect(container.firstChild).toBeNull()
    },
  )
})
