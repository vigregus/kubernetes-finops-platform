import { cleanup, fireEvent, render } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { IDLE, type CallView } from "./callState"
import { CallOverlay } from "./CallOverlay"
import { CallsContext, type CallsContextValue } from "./CallsProvider"
import type { VideoQuality } from "./callEngine"

afterEach(cleanup)

function value(overrides: Partial<CallsContextValue> = {}, view: Partial<CallView> = {}): CallsContextValue {
  return {
    view: { ...IDLE, phase: "active", callId: "c", role: "caller", peerName: "Bob", kind: "video", ...view },
    localStream: null,
    remoteStream: null,
    signalingUp: true,
    supported: true,
    notice: null,
    videoQuality: "auto",
    startCall: vi.fn(),
    accept: vi.fn(),
    decline: vi.fn(),
    hangup: vi.fn(),
    setMuted: vi.fn(),
    setCameraOff: vi.fn(),
    setVideoQuality: vi.fn(),
    hasVideo: () => true,
    ...overrides,
  }
}

function renderWith(ctx: CallsContextValue) {
  return render(
    <CallsContext.Provider value={ctx}>
      <CallOverlay />
    </CallsContext.Provider>,
  )
}

describe("выбор качества видео на экране звонка", () => {
  it("в видеозвонке кнопка качества есть, в аудиозвонке нет", () => {
    const video = renderWith(value())
    expect(video.queryByRole("button", { name: "Video quality" })).not.toBeNull()
    video.unmount()

    const audio = renderWith(value({}, { kind: "audio" }))
    expect(audio.queryByRole("button", { name: "Video quality" })).toBeNull()
  })

  it("меню открывается и предлагает Auto и три пресета, текущий отмечен", () => {
    const { getByRole, getAllByRole, container } = renderWith(value({ videoQuality: "medium" }))
    fireEvent.click(getByRole("button", { name: "Video quality" }))
    const items = getAllByRole("menuitemradio")
    // Отмеченный пункт несёт ещё и значок галочки: сравниваем подписи без него.
    expect(items.map((item) => item.textContent?.replace("check", ""))).toEqual([
      "Auto",
      "Low · 360p",
      "Medium · 480p",
      "High · 720p",
    ])
    expect(items.map((item) => item.getAttribute("aria-checked"))).toEqual(["false", "false", "true", "false"])
    expect(container.querySelector("[data-call-quality='medium']")).not.toBeNull()
  })

  it.each<[string, VideoQuality]>([
    ["Auto", "auto"],
    ["Low · 360p", "low"],
    ["Medium · 480p", "medium"],
    ["High · 720p", "high"],
  ])("выбор «%s» вызывает смену качества и закрывает меню", (label, expected) => {
    const ctx = value()
    const { getByRole, queryByRole } = renderWith(ctx)
    fireEvent.click(getByRole("button", { name: "Video quality" }))
    fireEvent.click(getByRole("menuitemradio", { name: new RegExp(`^${label.replace("·", "·")}`) }))
    expect(ctx.setVideoQuality).toHaveBeenCalledWith(expected)
    expect(queryByRole("menu")).toBeNull()
  })

  it("меню подписано как касающееся исходящего видео", () => {
    const { getByRole, getByText } = renderWith(value())
    fireEvent.click(getByRole("button", { name: "Video quality" }))
    expect(getByText("Your outgoing video")).toBeTruthy()
  })
})
