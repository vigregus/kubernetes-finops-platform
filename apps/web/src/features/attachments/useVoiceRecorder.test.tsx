import { act, renderHook, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { RecorderLike, VoiceDeps } from "./voiceRecorder"
import { useVoiceRecorder } from "./useVoiceRecorder"

class FakeRecorder implements RecorderLike {
  ondataavailable: RecorderLike["ondataavailable"] = null
  onstop: RecorderLike["onstop"] = null
  mimeType = "audio/webm;codecs=opus"
  start() {}
  stop() {
    this.ondataavailable?.({ data: new Blob([new Uint8Array(10)]) })
    this.onstop?.()
  }
}

function deps(getUserMedia?: VoiceDeps["getUserMedia"]): VoiceDeps {
  const stop = vi.fn()
  const stream = { getTracks: () => [{ stop }] } as unknown as MediaStream
  let t = 0
  return {
    getUserMedia: getUserMedia ?? (async () => stream),
    createRecorder: () => new FakeRecorder(),
    isTypeSupported: () => true,
    now: () => (t += 3000),
  }
}

describe("useVoiceRecorder", () => {
  it("запись → остановка отдаёт файл нужного типа и длительность", async () => {
    const onRecorded = vi.fn()
    const { result } = renderHook(() => useVoiceRecorder(deps(), onRecorded))
    act(() => result.current.start())
    await waitFor(() => expect(result.current.voice.state).toBe("recording"))
    act(() => result.current.stop())
    await waitFor(() => expect(onRecorded).toHaveBeenCalledTimes(1))

    const [file, meta] = onRecorded.mock.calls[0]
    expect(file.type).toBe("audio/webm")
    expect(file.name).toBe("voice.webm")
    expect(meta.durationMs).toBeGreaterThan(0)
    expect(result.current.voice.state).toBe("idle")
  })

  it("отмена ничего не отдаёт", async () => {
    const onRecorded = vi.fn()
    const { result } = renderHook(() => useVoiceRecorder(deps(), onRecorded))
    act(() => result.current.start())
    await waitFor(() => expect(result.current.voice.state).toBe("recording"))
    act(() => result.current.cancel())
    expect(result.current.voice.state).toBe("idle")
    expect(onRecorded).not.toHaveBeenCalled()
  })

  it("запрет микрофона показывается словами, а не молчанием", async () => {
    const denied = deps(async () => {
      throw Object.assign(new Error("x"), { name: "NotAllowedError" })
    })
    const { result } = renderHook(() => useVoiceRecorder(denied, vi.fn()))
    act(() => result.current.start())
    await waitFor(() => expect(result.current.voice.state).toBe("failed"))
    expect(result.current.voice).toMatchObject({ message: expect.stringContaining("Microphone") })
  })

  it("без браузерной записи запуск ничего не делает", () => {
    const { result } = renderHook(() => useVoiceRecorder(null, vi.fn()))
    act(() => result.current.start())
    expect(result.current.voice.state).toBe("idle")
  })
})
