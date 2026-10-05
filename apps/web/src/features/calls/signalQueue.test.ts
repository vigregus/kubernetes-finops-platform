import { describe, expect, it } from "vitest"

import { createSerialQueue, withRetry } from "./signalQueue"

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


describe("повтор отправки", () => {
  const instant = async () => undefined

  it("после сбоя повторяет и доводит до успеха", async () => {
    let calls = 0
    await withRetry(
      async () => {
        calls += 1
        if (calls < 3) throw new Error("503")
      },
      { sleep: instant },
    )
    expect(calls).toBe(3)
  })

  it("число повторов ограничено, последняя ошибка возвращается", async () => {
    let calls = 0
    await expect(
      withRetry(
        async () => {
          calls += 1
          throw new Error(`сбой ${calls}`)
        },
        { sleep: instant, delaysMs: [1, 1] },
      ),
    ).rejects.toThrow("сбой 3")
    expect(calls).toBe(3)
  })

  it("отказ клиента (не повторяемая ошибка) не повторяется", async () => {
    let calls = 0
    await expect(
      withRetry(
        async () => {
          calls += 1
          throw new Error("400")
        },
        { sleep: instant, retryable: () => false },
      ),
    ).rejects.toThrow("400")
    expect(calls).toBe(1)
  })

  it("паузы идут по возрастанию, как заданы", async () => {
    const slept: number[] = []
    let calls = 0
    await withRetry(
      async () => {
        calls += 1
        if (calls < 3) throw new Error("x")
      },
      { sleep: async (ms) => void slept.push(ms), delaysMs: [100, 300] },
    )
    expect(slept).toEqual([100, 300])
  })
})
