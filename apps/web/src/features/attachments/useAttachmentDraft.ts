/**
 * Черновик вложения в композере: выбранный файл и его путь до `ready`.
 *
 * Состояние одно на панель беседы (панель пересоздаётся по `key`), поэтому
 * черновик не переживает смену беседы — как и несохранённый текст в поле.
 */
import { useCallback, useEffect, useRef, useState } from "react"

import {
  failureMessage,
  uploadAttachment,
  type AttachmentOps,
  type UploadOptions,
} from "./attachmentUpload"

export type AttachmentDraft =
  | { readonly state: "empty" }
  | { readonly state: "uploading"; readonly fileName: string }
  | { readonly state: "processing"; readonly fileName: string }
  | {
      readonly state: "ready"
      readonly fileName: string
      readonly attachmentId: string
      readonly kind: "image" | "file"
    }
  | { readonly state: "failed"; readonly fileName: string; readonly message: string }

export interface UseAttachmentDraft {
  readonly draft: AttachmentDraft
  pick(file: File): void
  clear(): void
  /**
   * Снять черновик, только если он всё ещё **этот** готовый файл. Ответ на
   * отправку приходит позже, чем человек успел выбрать следующий файл, и
   * безусловный `clear()` стёр бы уже новый черновик.
   */
  clearIfReady(attachmentId: string): void
}

export function useAttachmentDraft(
  ops: AttachmentOps | undefined,
  options: UploadOptions = {},
): UseAttachmentDraft {
  const [draft, setDraft] = useState<AttachmentDraft>({ state: "empty" })
  // Номер выбора: ответ на прежний выбор, пришедший после нового, не должен
  // затереть новый черновик.
  const generation = useRef(0)
  const mounted = useRef(true)
  const optionsRef = useRef(options)
  useEffect(() => {
    optionsRef.current = options
  })
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const pick = useCallback(
    (file: File) => {
      if (ops === undefined) return
      generation.current += 1
      const mine = generation.current
      setDraft({ state: "uploading", fileName: file.name })

      void uploadAttachment(
        ops,
        file,
        (progress) => {
          if (mine === generation.current && mounted.current) {
            setDraft({ state: progress, fileName: file.name })
          }
        },
        optionsRef.current,
      ).then((outcome) => {
        if (mine !== generation.current || !mounted.current) return
        if (outcome.kind === "ready") {
          setDraft({
            state: "ready",
            fileName: file.name,
            attachmentId: outcome.attachmentId,
            kind: file.type.startsWith("image/") ? "image" : "file",
          })
        } else {
          setDraft({ state: "failed", fileName: file.name, message: failureMessage(outcome.failure) })
        }
      })
    },
    [ops],
  )

  const clear = useCallback(() => {
    generation.current += 1
    setDraft({ state: "empty" })
  }, [])

  const clearIfReady = useCallback((attachmentId: string) => {
    setDraft((current) => {
      if (current.state !== "ready" || current.attachmentId !== attachmentId) return current
      generation.current += 1
      return { state: "empty" }
    })
  }, [])

  return { draft, pick, clear, clearIfReady }
}
