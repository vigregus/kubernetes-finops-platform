import { describe, expect, it } from "vitest"

import { createTypingSender, type TypingSignal } from "./typingSender"

function setup(heartbeatMs = 2500) {
  const sent: TypingSignal[] = []
  const clock = { now: 0 }
  const sender = createTypingSender({ send: (s) => sent.push(s), now: () => clock.now, heartbeatMs })
  return { sender, sent, clock }
}

describe("createTypingSender", () => {
  it("первое нажатие — сразу", () => {
    const { sender, sent } = setup()
    sender.input(true)
    expect(sent).toEqual(["typing"])
  })

  it("быстрая печать не заливает канал: не чаще одного сердцебиения за период", () => {
    const { sender, sent, clock } = setup()
    for (let t = 0; t < 2400; t += 50) {
      clock.now = t
      sender.input(true)
    }
    expect(sent).toEqual(["typing"])
    clock.now = 2500
    sender.input(true)
    expect(sent).toEqual(["typing", "typing"])
  })

  it("очистка поля — stop, и повторная очистка ничего не шлёт", () => {
    const { sender, sent } = setup()
    sender.input(true)
    sender.input(false)
    sender.input(false)
    expect(sent).toEqual(["typing", "stop"])
  })

  it("после stop следующий набор снова начинается сразу", () => {
    const { sender, sent, clock } = setup()
    sender.input(true)
    sender.stop()
    clock.now = 10
    sender.input(true)
    expect(sent).toEqual(["typing", "stop", "typing"])
  })

  it("stop без набора ничего не шлёт", () => {
    const { sender, sent } = setup()
    sender.stop()
    expect(sent).toEqual([])
  })
})
