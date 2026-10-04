import { act, renderHook, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { AttachmentOps } from "./attachmentUpload"
import { useAttachmentDraft } from "./useAttachmentDraft"

const ID = "44444444-4444-4444-4444-444444444444"

function ops(): AttachmentOps {
  return {
    create: vi.fn(async () => ({ attachmentId: ID, uploadUrl: "u", expiresAt: "t", uploadHeaders: {} })),
    complete: vi.fn(async () => ({ attachmentId: ID, state: "ready" }) as never),
    status: vi.fn(async () => ({ attachmentId: ID, state: "ready" }) as never),
    put: vi.fn(async () => ({ ok: true })),
  }
}

const file = (name = "a.png", type = "image/png") => new File(["x"], name, { type })

describe("useAttachmentDraft", () => {
  it("доводит выбранный файл до ready и называет вид", async () => {
    const { result } = renderHook(() => useAttachmentDraft(ops(), { sleep: async () => {} }))
    act(() => result.current.pick(file()))
    await waitFor(() => expect(result.current.draft.state).toBe("ready"))
    expect(result.current.draft).toMatchObject({ attachmentId: ID, kind: "image", fileName: "a.png" })
  })

  it("без операций вложение не выбирается вовсе", () => {
    const { result } = renderHook(() => useAttachmentDraft(undefined))
    act(() => result.current.pick(file()))
    expect(result.current.draft.state).toBe("empty")
  })

  it("запоздалый ответ на отправку не стирает следующий черновик", async () => {
    const { result } = renderHook(() => useAttachmentDraft(ops(), { sleep: async () => {} }))
    act(() => result.current.pick(file()))
    await waitFor(() => expect(result.current.draft.state).toBe("ready"))

    // Человек успел выбрать другой файл, и только потом пришёл ответ на отправку
    // предыдущего: он снимает черновик по **прежнему** вложению.
    const failing: AttachmentOps = {
      ...ops(),
      create: vi.fn(async () => Promise.reject({ status: 415 })),
    }
    const second = renderHook(() => useAttachmentDraft(failing, { sleep: async () => {} }))
    act(() => second.result.current.pick(file("x.exe", "application/x-msdownload")))
    await waitFor(() => expect(second.result.current.draft.state).toBe("failed"))
    act(() => second.result.current.clearIfReady(ID))
    expect(second.result.current.draft.state).toBe("failed")

    act(() => result.current.clearIfReady(ID))
    expect(result.current.draft.state).toBe("empty")
  })
})
