/**
 * Загрузка вложения: инициация → `PUT` в хранилище → `complete` → опрос.
 *
 * Функция без React и без глобальных зависимостей: операции приходят
 * параметрами, поэтому каждый исход (отказ по типу, потерянный `PUT`,
 * отклонение после обработки) предъявляется прогоном без сети.
 *
 * `PUT` идёт **не** через клиент API: ему нельзя ни `Authorization`, ни
 * cookie — ссылка уже подписана, и лишний заголовок (или `credentials`)
 * превратил бы запрос в чужой для хранилища. Заголовки берутся из ответа
 * инициации как есть: они входят в подпись.
 */
import type { AttachmentStatus, CreateAttachment201Response, Message } from "../../api/generated"

/** Что сервер называет причиной отказа (`rejection_code`) и что показать человеку. */
export type UploadFailure =
  | { readonly kind: "type-not-allowed" }
  | { readonly kind: "too-large" }
  | { readonly kind: "rate-limited" }
  | { readonly kind: "invalid-voice" }
  | { readonly kind: "unavailable" }
  | { readonly kind: "upload-failed" }
  | { readonly kind: "rejected"; readonly code: string | undefined }
  | { readonly kind: "timeout" }

export type UploadProgress = "uploading" | "processing"

export type UploadOutcome =
  | { readonly kind: "ready"; readonly attachmentId: string }
  | { readonly kind: "failed"; readonly failure: UploadFailure }

export interface AttachmentOps {
  readonly create: (request: {
    contentType: string
    sizeBytes: number
    fileName: string
    /** Только у голосового: длительность, которую показал рекордер (`ATT-004`). */
    durationMs?: number
  }) => Promise<CreateAttachment201Response>
  readonly complete: (attachmentId: string) => Promise<AttachmentStatus>
  readonly status: (attachmentId: string) => Promise<AttachmentStatus>
  /** `fetch` без учётных данных и без заголовков приложения. */
  readonly put: (url: string, headers: Record<string, string>, body: Blob) => Promise<{ ok: boolean }>
}

export interface UploadOptions {
  /** Длительность голосового, мс: уходит в инициацию и проверяется сервером. */
  readonly durationMs?: number
  readonly pollIntervalMs?: number
  readonly timeoutMs?: number
  readonly sleep?: (ms: number) => Promise<void>
  readonly now?: () => number
  readonly signal?: AbortSignal
}

const DEFAULT_POLL_MS = 1000
const DEFAULT_TIMEOUT_MS = 2 * 60 * 1000

/** Отказ API → понятная причина. Статус читается по полю, которое кладёт `ApiProblem`. */
function failureOfError(error: unknown): UploadFailure {
  const status =
    typeof error === "object" && error !== null && "status" in error
      ? (error as { status?: unknown }).status
      : undefined
  if (status === 415) return { kind: "type-not-allowed" }
  if (status === 413) return { kind: "too-large" }
  if (status === 400) return { kind: "invalid-voice" }
  if (status === 429) return { kind: "rate-limited" }
  return { kind: "unavailable" }
}

export async function uploadAttachment(
  ops: AttachmentOps,
  file: File,
  onProgress: (progress: UploadProgress) => void,
  options: UploadOptions = {},
): Promise<UploadOutcome> {
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)))
  const now = options.now ?? (() => Date.now())

  onProgress("uploading")

  let init: CreateAttachment201Response
  try {
    init = await ops.create({
      contentType: file.type,
      sizeBytes: file.size,
      fileName: file.name,
      ...(options.durationMs === undefined ? {} : { durationMs: options.durationMs }),
    })
  } catch (error) {
    return { kind: "failed", failure: failureOfError(error) }
  }

  try {
    const put = await ops.put(init.uploadUrl, init.uploadHeaders ?? {}, file)
    if (!put.ok) return { kind: "failed", failure: { kind: "upload-failed" } }
  } catch {
    return { kind: "failed", failure: { kind: "upload-failed" } }
  }

  onProgress("processing")

  let current: AttachmentStatus
  try {
    current = await ops.complete(init.attachmentId)
  } catch (error) {
    return { kind: "failed", failure: failureOfError(error) }
  }

  const deadline = now() + (options.timeoutMs ?? DEFAULT_TIMEOUT_MS)
  for (;;) {
    if (options.signal?.aborted) return { kind: "failed", failure: { kind: "unavailable" } }
    if (current.state === "ready") return { kind: "ready", attachmentId: init.attachmentId }
    if (current.state === "rejected" || current.state === "failed") {
      return { kind: "failed", failure: { kind: "rejected", code: current.rejectionCode } }
    }
    if (now() >= deadline) return { kind: "failed", failure: { kind: "timeout" } }

    await sleep(options.pollIntervalMs ?? DEFAULT_POLL_MS)
    try {
      current = await ops.status(init.attachmentId)
    } catch (error) {
      return { kind: "failed", failure: failureOfError(error) }
    }
  }
}

/** Что сказать человеку. Слова — здесь, а не в разметке: их проверяет тест. */
export function failureMessage(failure: UploadFailure): string {
  switch (failure.kind) {
    case "type-not-allowed":
      return "This file type isn't supported."
    case "too-large":
      return "This file is too large."
    case "rate-limited":
      return "Too many uploads. Try again in a moment."
    case "invalid-voice":
      return "This recording can't be sent. Try recording again."
    case "unavailable":
      return "Attachments are unavailable right now."
    case "upload-failed":
      return "Upload failed. Try again."
    case "timeout":
      return "Processing is taking too long. Try again."
    case "rejected":
      return failure.code === "malware"
        ? "This file was blocked as unsafe."
        : failure.code === "type_mismatch"
          ? "The file doesn't match its type."
          : failure.code === "invalid_audio"
            ? "This recording can't be sent. Try recording again."
            : "This file was rejected."
  }
}

/** Отправка сообщения-вложения: готовое вложение + необязательная подпись. */
export type SendAttachmentMessage = (request: {
  readonly conversationId: string
  /** Тождество логической отправки: повтор после сбоя шлёт **тот же** (D3). */
  readonly clientMessageId: string
  readonly kind: "image" | "file" | "voice"
  readonly caption: string
  /** Только у голосового: уходит в `payload.duration_ms`, сервер берёт своё. */
  readonly durationMs?: number
  readonly attachmentId: string
}) => Promise<Message>

export interface AttachmentClient {
  readonly ops: AttachmentOps
  readonly send: SendAttachmentMessage
}
