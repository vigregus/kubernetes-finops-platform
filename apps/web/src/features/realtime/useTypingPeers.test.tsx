import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useTypingPeers } from "./useTypingPeers"

beforeEach(() => vi.useFakeTimers({ now: 1_000_000 }))
afterEach(() => vi.useRealTimers())

const event = (userId: string, expiresInMs = 5000) => ({ user_id: userId, expires_in_ms: expiresInMs })

describe("useTypingPeers", () => {
  it("показывает печатающего и гасит его сам, без stop (RT-001)", () => {
    const { result } = renderHook(() => useTypingPeers("me"))
    act(() => result.current.onPublication(event("a")))
    expect(result.current.userIds).toEqual(["a"])

    act(() => {
      vi.advanceTimersByTime(4000)
    })
    expect(result.current.userIds).toEqual(["a"])
    act(() => {
      vi.advanceTimersByTime(1100)
    })
    expect(result.current.userIds).toEqual([])
  })

  it("сердцебиение продлевает, stop гасит сразу", () => {
    const { result } = renderHook(() => useTypingPeers("me"))
    act(() => result.current.onPublication(event("a")))
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    act(() => result.current.onPublication(event("a")))
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(result.current.userIds).toEqual(["a"])
    act(() => result.current.onPublication(event("a", 0)))
    expect(result.current.userIds).toEqual([])
  })

  it("своё событие и чужая форма не показываются", () => {
    const { result } = renderHook(() => useTypingPeers("me"))
    act(() => result.current.onPublication(event("me")))
    act(() => result.current.onPublication({ state: "typing" }))
    act(() => result.current.onPublication(null))
    expect(result.current.userIds).toEqual([])
  })
})
