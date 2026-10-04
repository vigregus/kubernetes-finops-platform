import { describe, expect, it, vi } from "vitest"

import {
  baseMimeType,
  pickMimeType,
  startRecording,
  VoiceError,
  voiceFailureMessage,
  type RecorderLike,
  type VoiceDeps,
} from "./voiceRecorder"

function fakeStream() {
  const stop = vi.fn()
  return { stream: { getTracks: () => [{ stop }, { stop }] } as unknown as MediaStream, stop }
}

class FakeRecorder implements RecorderLike {
  ondataavailable: RecorderLike["ondataavailable"] = null
  onstop: RecorderLike["onstop"] = null
  started = false
  readonly mimeType: string
  constructor(mimeType: string) {
    this.mimeType = mimeType
  }
  start() {
    this.started = true
  }
  stop() {
    this.ondataavailable?.({ data: new Blob([new Uint8Array(40)], { type: this.mimeType }) })
    this.onstop?.()
  }
}

function deps(overrides: Partial<VoiceDeps> = {}, mime = "audio/webm;codecs=opus") {
  const { stream, stop } = fakeStream()
  const clock = { value: 1000 }
  const recorder = new FakeRecorder(mime)
  const value: VoiceDeps = {
    getUserMedia: async () => stream,
    createRecorder: () => recorder,
    isTypeSupported: () => true,
    now: () => clock.value,
    ...overrides,
  }
  return { value, recorder, stop, clock }
}

describe("выбор типа записи", () => {
  it("берёт первый поддерживаемый из предпочтительных", () => {
    expect(pickMimeType((t) => t === "audio/mp4")).toBe("audio/mp4")
    expect(pickMimeType(() => true)).toBe("audio/webm;codecs=opus")
    expect(pickMimeType(() => false)).toBeUndefined()
  })

  it("отбрасывает параметры кодека: сервер знает тип без них", () => {
    expect(baseMimeType("audio/webm;codecs=opus")).toBe("audio/webm")
    expect(baseMimeType("Audio/OGG; codecs=opus")).toBe("audio/ogg")
  })
})

describe("startRecording", () => {
  it("записывает, считает длительность по часам и отдаёт тип без кодека", async () => {
    const d = deps()
    const handle = await startRecording(d.value)
    expect(d.recorder.started).toBe(true)
    d.clock.value = 8200
    const recording = await handle.stop()

    expect(recording.durationMs).toBe(7200)
    expect(recording.mimeType).toBe("audio/webm")
    expect(recording.blob.size).toBe(40)
  })

  it("освобождает микрофон и при окончании, и при отмене", async () => {
    const finished = deps()
    await (await startRecording(finished.value)).stop()
    expect(finished.stop).toHaveBeenCalledTimes(2)

    const cancelled = deps()
    ;(await startRecording(cancelled.value)).cancel()
    expect(cancelled.stop).toHaveBeenCalledTimes(2)
  })

  it("запрет микрофона — отдельная причина", async () => {
    const denied = deps({
      getUserMedia: async () => {
        throw Object.assign(new Error("x"), { name: "NotAllowedError" })
      },
    })
    await expect(startRecording(denied.value)).rejects.toMatchObject({
      failure: { kind: "permission-denied" },
    })
  })

  it("нет устройства — отдельная причина", async () => {
    const none = deps({
      getUserMedia: async () => {
        throw Object.assign(new Error("x"), { name: "NotFoundError" })
      },
    })
    await expect(startRecording(none.value)).rejects.toBeInstanceOf(VoiceError)
    await expect(startRecording(none.value)).rejects.toMatchObject({
      failure: { kind: "no-microphone" },
    })
  })

  it("если рекордер не создаётся, микрофон всё равно освобождён", async () => {
    const broken = deps({
      createRecorder: () => {
        throw new Error("no recorder")
      },
    })
    await expect(startRecording(broken.value)).rejects.toMatchObject({
      failure: { kind: "unsupported" },
    })
    expect(broken.stop).toHaveBeenCalledTimes(2)
  })

  it("слова для человека названы по каждой причине", () => {
    for (const kind of ["permission-denied", "no-microphone", "unsupported"] as const) {
      expect(voiceFailureMessage({ kind }).length).toBeGreaterThan(10)
    }
  })
})
