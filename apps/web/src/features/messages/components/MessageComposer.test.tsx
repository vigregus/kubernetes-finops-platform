import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { AttachmentDraft } from "../../attachments/useAttachmentDraft"
import { MessageComposer } from "./MessageComposer"

afterEach(cleanup)

function withDraft(draft: AttachmentDraft) {
  const onSend = vi.fn()
  const attachment = { draft, onPick: vi.fn(), onClear: vi.fn(), onSend: vi.fn() }
  const view = render(<MessageComposer recipientName="Аня" onSend={onSend} attachment={attachment} />)
  return { onSend, attachment, view }
}

const input = () => document.querySelector("[data-composer-input]") as HTMLInputElement
const send = () => document.querySelector("[data-composer-send]") as HTMLButtonElement

describe("композер с вложением", () => {
  it("без пропса вложения кнопки скрепки нет — композер прежний", () => {
    render(<MessageComposer recipientName="Аня" onSend={vi.fn()} />)
    expect(document.querySelector("[data-composer-attach]")).toBeNull()
  })

  it("готовое вложение уходит с подписью, а не текстом", () => {
    const { onSend, attachment } = withDraft({
      state: "ready", fileName: "кот.png", attachmentId: "a-1", kind: "image",
    })
    fireEvent.change(input(), { target: { value: "смотри" } })
    fireEvent.click(send())

    expect(attachment.onSend).toHaveBeenCalledWith("смотри")
    expect(onSend).not.toHaveBeenCalled()
  })

  it("готовое вложение уходит и без подписи", () => {
    const { attachment } = withDraft({
      state: "ready", fileName: "a.pdf", attachmentId: "a-2", kind: "file",
    })
    fireEvent.click(send())
    expect(attachment.onSend).toHaveBeenCalledWith("")
  })

  it.each(["uploading", "processing"] as const)("пока файл %s, текст отдельно не уходит", (state) => {
    const { onSend, attachment } = withDraft({ state, fileName: "a.png" })
    fireEvent.change(input(), { target: { value: "привет" } })
    fireEvent.click(send())

    expect(onSend).not.toHaveBeenCalled()
    expect(attachment.onSend).not.toHaveBeenCalled()
  })

  it("отказ показывает причину и даёт убрать вложение", () => {
    const { attachment } = withDraft({ state: "failed", fileName: "x.exe", message: "This file type isn't supported." })
    expect(screen.getByText("This file type isn't supported.")).toBeTruthy()
    fireEvent.click(document.querySelector("[data-attachment-clear]")!)
    expect(attachment.onClear).toHaveBeenCalled()
  })

  it("выбранный файл уходит наверх и сбрасывает поле выбора", () => {
    const { attachment } = withDraft({ state: "empty" })
    const file = new File(["x"], "a.png", { type: "image/png" })
    const field = document.querySelector("[data-composer-file]") as HTMLInputElement
    fireEvent.change(field, { target: { files: [file] } })
    expect(attachment.onPick).toHaveBeenCalledWith(file)
  })
})


describe("композер сообщает «печатает»", () => {
  it("набор текста — true, очистка поля — false", () => {
    const onTyping = vi.fn()
    render(<MessageComposer recipientName="Аня" onSend={vi.fn()} onTyping={onTyping} />)
    fireEvent.change(input(), { target: { value: "при" } })
    fireEvent.change(input(), { target: { value: "" } })
    expect(onTyping.mock.calls).toEqual([[true], [false]])
  })

  it("пробелы вместо текста — не набор", () => {
    const onTyping = vi.fn()
    render(<MessageComposer recipientName="Аня" onSend={vi.fn()} onTyping={onTyping} />)
    fireEvent.change(input(), { target: { value: "   " } })
    expect(onTyping).toHaveBeenLastCalledWith(false)
  })

  it("отправка гасит набор: собеседник не видит его после ухода сообщения", () => {
    const onTyping = vi.fn()
    render(<MessageComposer recipientName="Аня" onSend={vi.fn()} onTyping={onTyping} />)
    fireEvent.change(input(), { target: { value: "привет" } })
    fireEvent.click(send())
    expect(onTyping).toHaveBeenLastCalledWith(false)
    expect(onTyping.mock.calls.length).toBe(2)
  })

  it("без обработчика композер прежний", () => {
    render(<MessageComposer recipientName="Аня" onSend={vi.fn()} />)
    expect(() => fireEvent.change(input(), { target: { value: "x" } })).not.toThrow()
  })
})
