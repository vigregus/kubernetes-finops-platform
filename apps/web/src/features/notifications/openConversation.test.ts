import { describe, expect, it } from "vitest"

import { conversationFromUrl } from "./openConversation"

describe("conversationFromUrl", () => {
  it("беседа из адреса, открытого нажатием на уведомление (NTF-008)", () => {
    expect(conversationFromUrl("?conversation=c-1")).toBe("c-1")
    expect(conversationFromUrl("?x=1&conversation=a%20b")).toBe("a b")
  })

  it("нет параметра или он пуст — беседы нет", () => {
    expect(conversationFromUrl("")).toBeNull()
    expect(conversationFromUrl("?conversation=")).toBeNull()
    expect(conversationFromUrl("?other=1")).toBeNull()
  })
})
