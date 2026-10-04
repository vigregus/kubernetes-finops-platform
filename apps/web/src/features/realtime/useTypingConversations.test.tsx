import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useTypingConversations } from "./useTypingConversations"

beforeEach(() => vi.useFakeTimers({ now: 1_000_000 }))
afterEach(() => vi.useRealTimers())

const event = (userId: string, expiresInMs = 5000) => ({ user_id: userId, expires_in_ms: expiresInMs })
const A = "typing:conv-a"
const B = "typing:conv-b"

describe("useTypingConversations", () => {
  it("ведёт каждую беседу отдельно — и открытую, и закрытую", () => {
    const { result } = renderHook(() => useTypingConversations("me"))
    act(() => result.current.onPublication(A, event("u1")))
    act(() => result.current.onPublication(B, event("u2")))
    expect(result.current.byConversation.get("conv-a")).toEqual(["u1"])
    expect(result.current.byConversation.get("conv-b")).toEqual(["u2"])
  })

  it("гаснет сам без stop (RT-001), а соседняя беседа остаётся", () => {
    const { result } = renderHook(() => useTypingConversations("me"))
    act(() => result.current.onPublication(A, event("u1", 2000)))
    act(() => result.current.onPublication(B, event("u2", 9000)))
    act(() => {
      vi.advanceTimersByTime(2100)
    })
    expect(result.current.byConversation.has("conv-a")).toBe(false)
    expect(result.current.byConversation.get("conv-b")).toEqual(["u2"])
  })

  it("stop снимает сразу и убирает беседу из карты", () => {
    const { result } = renderHook(() => useTypingConversations("me"))
    act(() => result.current.onPublication(A, event("u1")))
    act(() => result.current.onPublication(A, event("u1", 0)))
    expect(result.current.byConversation.size).toBe(0)
  })

  it("своё событие, чужой канал и чужая форма не показываются", () => {
    const { result } = renderHook(() => useTypingConversations("me"))
    act(() => result.current.onPublication(A, event("me")))
    act(() => result.current.onPublication("conversation:conv-a", event("u1")))
    act(() => result.current.onPublication("typing:", event("u1")))
    act(() => result.current.onPublication(A, { state: "typing" }))
    expect(result.current.byConversation.size).toBe(0)
  })
})
