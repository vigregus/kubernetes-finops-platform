import { describe, expect, it, vi } from "vitest"

import type { AttachmentStatus } from "../../api/generated"
import { failureMessage, uploadAttachment, type AttachmentOps } from "./attachmentUpload"

const FILE = new File([new Uint8Array(8)], "кот.png", { type: "image/png" })
const ID = "44444444-4444-4444-4444-444444444444"

const status = (state: AttachmentStatus["state"], rejectionCode?: string): AttachmentStatus =>
  ({ attachmentId: ID, state, rejectionCode }) as AttachmentStatus

function ops(overrides: Partial<AttachmentOps> = {}): AttachmentOps {
  return {
    create: vi.fn(async () => ({
      attachmentId: ID,
      uploadUrl: "https://s3.finops.local/put",
      expiresAt: "2026-01-01T00:00:00Z",
      uploadHeaders: { "Content-Type": "image/png" },
    })),
    complete: vi.fn(async () => status("processing")),
    status: vi.fn(async () => status("ready")),
    put: vi.fn(async () => ({ ok: true })),
    ...overrides,
  }
}

const fast = { sleep: async () => {}, pollIntervalMs: 0 }

class Problem extends Error {
  readonly status: number
  constructor(status: number) {
    super("problem")
    this.status = status
  }
}

describe("uploadAttachment", () => {
  it("проходит путь целиком и называет стадии по порядку", async () => {
    const seen: string[] = []
    const o = ops()
    const outcome = await uploadAttachment(o, FILE, (p) => seen.push(p), fast)

    expect(outcome).toEqual({ kind: "ready", attachmentId: ID })
    expect(seen).toEqual(["uploading", "processing"])
    expect(o.create).toHaveBeenCalledWith({ contentType: "image/png", sizeBytes: 8, fileName: "кот.png" })
  })

  it("длительность голосового уходит в инициацию", async () => {
    const voice = new File([new Uint8Array(40)], "voice.webm", { type: "audio/webm" })
    const o = ops()
    await uploadAttachment(o, voice, () => {}, { ...fast, durationMs: 7200 })
    expect(o.create).toHaveBeenCalledWith({
      contentType: "audio/webm", sizeBytes: 40, fileName: "voice.webm", durationMs: 7200,
    })
  })

  it("отказ сервера по длительности называется своей причиной", async () => {
    const voice = new File([new Uint8Array(40)], "voice.webm", { type: "audio/webm" })
    const o = ops({ create: vi.fn(async () => { throw new Problem(400) }) })
    const outcome = await uploadAttachment(o, voice, () => {}, { ...fast, durationMs: 7200 })
    expect(outcome).toEqual({ kind: "failed", failure: { kind: "invalid-voice" } })
  })

  it("кладёт в PUT заголовки из ответа инициации как есть", async () => {
    const o = ops()
    await uploadAttachment(o, FILE, () => {}, fast)
    expect(o.put).toHaveBeenCalledWith(
      "https://s3.finops.local/put",
      { "Content-Type": "image/png" },
      FILE,
    )
  })

  it.each([
    [415, "type-not-allowed"],
    [413, "too-large"],
    [429, "rate-limited"],
    [503, "unavailable"],
  ])("отказ инициации %i — это %s, и PUT не делается", async (code, kind) => {
    const o = ops({ create: vi.fn(async () => Promise.reject(new Problem(code))) })
    const outcome = await uploadAttachment(o, FILE, () => {}, fast)

    expect(outcome).toEqual({ kind: "failed", failure: { kind } })
    expect(o.put).not.toHaveBeenCalled()
  })

  it("потерянный PUT не доходит до complete", async () => {
    const o = ops({ put: vi.fn(async () => ({ ok: false })) })
    const outcome = await uploadAttachment(o, FILE, () => {}, fast)

    expect(outcome).toEqual({ kind: "failed", failure: { kind: "upload-failed" } })
    expect(o.complete).not.toHaveBeenCalled()
  })

  it("опрашивает, пока вложение processing", async () => {
    const statuses = [status("processing"), status("processing"), status("ready")]
    const o = ops({ status: vi.fn(async () => statuses.shift()!) })
    const outcome = await uploadAttachment(o, FILE, () => {}, fast)

    expect(outcome.kind).toBe("ready")
    expect(o.status).toHaveBeenCalledTimes(3)
  })

  it("отклонённое называет причину сервера", async () => {
    const o = ops({ status: vi.fn(async () => status("rejected", "malware")) })
    const outcome = await uploadAttachment(o, FILE, () => {}, fast)

    expect(outcome).toEqual({ kind: "failed", failure: { kind: "rejected", code: "malware" } })
  })

  it("не ждёт обработку вечно", async () => {
    let clock = 0
    const o = ops({ status: vi.fn(async () => status("processing")) })
    const outcome = await uploadAttachment(o, FILE, () => {}, {
      sleep: async () => {
        clock += 1000
      },
      now: () => clock,
      timeoutMs: 3000,
    })

    expect(outcome).toEqual({ kind: "failed", failure: { kind: "timeout" } })
  })

  it("слова для человека названы по каждой причине", () => {
    expect(failureMessage({ kind: "rejected", code: "malware" })).toMatch(/unsafe/)
    expect(failureMessage({ kind: "rejected", code: "type_mismatch" })).toMatch(/doesn't match/)
    expect(failureMessage({ kind: "too-large" })).toMatch(/too large/)
  })
})
