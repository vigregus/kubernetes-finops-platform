import { describe, expect, it } from "vitest"

import { createSerialQueue } from "./signalQueue"

describe("очередь сигналов", () => {
  it("медленный первый сигнал не пропускает быстрый второй вперёд себя", async () => {
    const started: string[] = []
    const finished: string[] = []
    const send = createSerialQueue<{ name: string; ms: number }>(async ({ name, ms }) => {
      started.push(name)
      await new Promise((resolve) => setTimeout(resolve, ms))
      finished.push(name)
    })
    await Promise.all([send({ name: "offer", ms: 30 }), send({ name: "ice", ms: 0 })])
    expect(started).toEqual(["offer", "ice"])
    expect(finished).toEqual(["offer", "ice"])
  })

  it("сбой одного сигнала не останавливает остальных и возвращается его отправителю", async () => {
    const sent: string[] = []
    const send = createSerialQueue<string>(async (name) => {
      if (name === "bad") throw new Error("сеть")
      sent.push(name)
    })
    const bad = send("bad")
    const good = send("good")
    await expect(bad).rejects.toThrow("сеть")
    await good
    expect(sent).toEqual(["good"])
  })
})
