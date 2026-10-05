import { describe, expect, it } from "vitest"

import { callActionFromUrl } from "./callActions"

describe("действие из адреса", () => {
  it.each([
    ["?call=abc&action=accept", { callId: "abc", action: "accept" }],
    ["?call=abc&action=decline", { callId: "abc", action: "decline" }],
    ["?call=abc&action=open", { callId: "abc", action: "open" }],
  ])("%s", (search, expected) => {
    expect(callActionFromUrl(search)).toEqual(expected)
  })

  it.each(["", "?call=abc", "?action=accept", "?call=&action=accept", "?call=abc&action=drop-db", "?call=abc&action="])(
    "негодное %j — не действие",
    (search) => {
      expect(callActionFromUrl(search)).toBeNull()
    },
  )
})
