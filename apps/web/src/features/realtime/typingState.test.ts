import { describe, expect, it } from "vitest"

import {
  activeUsers,
  applyTyping,
  EMPTY_TYPING,
  nextExpiry,
  parseTypingEvent,
  prune,
} from "./typingState"

const SELF = "self"

describe("parseTypingEvent", () => {
  it("разбирает событие сервера", () => {
    expect(parseTypingEvent({ user_id: "u1", expires_in_ms: 5000 })).toEqual({
      userId: "u1",
      expiresInMs: 5000,
    })
  })

  it("чужая форма отбрасывается целиком", () => {
    for (const bad of [null, "typing", 1, {}, { user_id: "u1" }, { expires_in_ms: 5 }, { user_id: "", expires_in_ms: 5 }, { user_id: "u1", expires_in_ms: -1 }, { user_id: "u1", expires_in_ms: "5" }, { user_id: 7, expires_in_ms: 5 }]) {
      expect(parseTypingEvent(bad)).toBeNull()
    }
  })
})

describe("applyTyping", () => {
  it("сердцебиение продлевает срок только своего пользователя", () => {
    let state = applyTyping(EMPTY_TYPING, { userId: "a", expiresInMs: 5000 }, 1000, SELF)
    state = applyTyping(state, { userId: "b", expiresInMs: 5000 }, 2000, SELF)
    state = applyTyping(state, { userId: "a", expiresInMs: 5000 }, 4000, SELF)
    expect(state.get("a")).toBe(9000)
    expect(state.get("b")).toBe(7000)
  })

  it("stop снимает сразу, не дожидаясь срока", () => {
    let state = applyTyping(EMPTY_TYPING, { userId: "a", expiresInMs: 5000 }, 0, SELF)
    state = applyTyping(state, { userId: "a", expiresInMs: 0 }, 100, SELF)
    expect(activeUsers(state, 100)).toEqual([])
  })

  it("своё собственное событие из другой вкладки не показывается", () => {
    const state = applyTyping(EMPTY_TYPING, { userId: SELF, expiresInMs: 5000 }, 0, SELF)
    expect(activeUsers(state, 1)).toEqual([])
  })
})

describe("срок", () => {
  it("индикатор гаснет сам: без сердцебиения запись истекает (RT-001)", () => {
    const state = applyTyping(EMPTY_TYPING, { userId: "a", expiresInMs: 5000 }, 0, SELF)
    expect(activeUsers(state, 4999)).toEqual(["a"])
    expect(activeUsers(state, 5000)).toEqual([])
  })

  it("истёкшее убирается, живое остаётся", () => {
    let state = applyTyping(EMPTY_TYPING, { userId: "a", expiresInMs: 1000 }, 0, SELF)
    state = applyTyping(state, { userId: "b", expiresInMs: 9000 }, 0, SELF)
    const pruned = prune(state, 5000)
    expect([...pruned.keys()]).toEqual(["b"])
    // Нечего убирать — та же ссылка: лишней перерисовки не будет.
    expect(prune(pruned, 5000)).toBe(pruned)
  })

  it("ближайшее истечение указывает, когда проснуться", () => {
    let state = applyTyping(EMPTY_TYPING, { userId: "a", expiresInMs: 3000 }, 0, SELF)
    state = applyTyping(state, { userId: "b", expiresInMs: 1000 }, 0, SELF)
    expect(nextExpiry(state)).toBe(1000)
    expect(nextExpiry(EMPTY_TYPING)).toBeNull()
  })
})
