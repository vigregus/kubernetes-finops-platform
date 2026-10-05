import { describe, expect, it } from "vitest"

import { callPreview, describeCall, formatCallDuration, isMissedFor, parseCallSummary } from "./callSummary"

describe("разбор кода из системного сообщения", () => {
  it("видит звонок и его вид", () => {
    expect(parseCallSummary("call.completed.video")).toEqual({ outcome: "completed", kind: "video" })
    expect(parseCallSummary("call.missed.audio")).toEqual({ outcome: "missed", kind: "audio" })
  })

  it.each([undefined, null, "", "hello", "call.", "call.exploded.audio", "call.completed.hologram", "xcall.completed.audio"])(
    "%j — не звонок",
    (value) => {
      expect(parseCallSummary(value)).toBeNull()
    },
  )
})

describe("подписи", () => {
  const done = parseCallSummary("call.completed.audio")!
  const missed = parseCallSummary("call.missed.video")!

  it("состоявшийся — с длительностью", () => {
    expect(describeCall(done, true, 75)).toBe("Voice call, 1:15")
    expect(describeCall(done, false)).toBe("Voice call")
  })

  it("пропущенный читается по-разному у звонящего и у адресата", () => {
    expect(describeCall(missed, true)).toBe("Video call · No answer")
    expect(describeCall(missed, false)).toBe("Missed video call")
  })

  it("красным подсвечивается только то, что пропустил читатель, и сбой", () => {
    expect(isMissedFor(missed, false)).toBe(true)
    expect(isMissedFor(missed, true)).toBe(false)
    expect(isMissedFor(done, false)).toBe(false)
    expect(isMissedFor(parseCallSummary("call.failed.audio")!, true)).toBe(true)
  })

  it("превью списка бесед коротко", () => {
    expect(callPreview(done, true)).toBe("Voice call")
    expect(callPreview(missed, false)).toBe("Missed call")
    expect(callPreview(parseCallSummary("call.declined.audio")!, true)).toBe("Call")
  })

  it("длительность без лишних нулей", () => {
    expect(formatCallDuration(5)).toBe("0:05")
    expect(formatCallDuration(754)).toBe("12:34")
    expect(formatCallDuration(3723)).toBe("1:02:03")
    expect(formatCallDuration(-4)).toBe("0:00")
  })
})
