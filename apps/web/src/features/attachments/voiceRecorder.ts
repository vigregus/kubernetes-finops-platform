/**
 * Запись голоса: `getUserMedia` + `MediaRecorder`, без React.
 *
 * Браузерные API приходят параметром (`VoiceDeps`), поэтому каждый исход —
 * отказ в микрофоне, нет устройства, браузер без записи — предъявляется
 * прогоном без камеры и без разрешений.
 *
 * Длительность считается **здесь**, по часам между началом и концом записи:
 * у `MediaRecorder` её нет, а в заголовке WebM, который он пишет, поля
 * длительности обычно нет вовсе (поток без финализации). Сервер не берёт её
 * из файла — он сверяет заявленную с размером (`ATT-004`).
 */

export type VoiceFailure =
  | { readonly kind: "permission-denied" }
  | { readonly kind: "no-microphone" }
  | { readonly kind: "unsupported" }

export interface VoiceRecording {
  readonly blob: Blob
  /** Тип без параметров кодека: `audio/webm`, а не `audio/webm;codecs=opus`. */
  readonly mimeType: string
  readonly durationMs: number
}

export interface RecorderHandle {
  /** Заканчивает запись и отдаёт её; освобождает микрофон. */
  stop(): Promise<VoiceRecording>
  /** Бросает запись без результата; освобождает микрофон. */
  cancel(): void
}

export interface RecorderLike {
  readonly mimeType: string
  ondataavailable: ((event: { data: Blob }) => void) | null
  onstop: (() => void) | null
  start(): void
  stop(): void
}

export interface VoiceDeps {
  readonly getUserMedia: () => Promise<MediaStream>
  readonly createRecorder: (stream: MediaStream, mimeType: string | undefined) => RecorderLike
  readonly isTypeSupported: (mimeType: string) => boolean
  readonly now: () => number
}

/** Что просим у браузера, по убыванию предпочтения. Совпадает с белым списком сервера. */
export const PREFERRED_TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"] as const

export function pickMimeType(isTypeSupported: (mimeType: string) => boolean): string | undefined {
  return PREFERRED_TYPES.find((candidate) => isTypeSupported(candidate))
}

export function baseMimeType(mimeType: string): string {
  return mimeType.split(";", 1)[0].trim().toLowerCase()
}

export class VoiceError extends Error {
  readonly failure: VoiceFailure
  constructor(failure: VoiceFailure) {
    super(failure.kind)
    this.failure = failure
  }
}

function failureOf(error: unknown): VoiceFailure {
  const name = typeof error === "object" && error !== null && "name" in error ? String((error as { name: unknown }).name) : ""
  if (name === "NotAllowedError" || name === "SecurityError") return { kind: "permission-denied" }
  if (name === "NotFoundError" || name === "OverconstrainedError") return { kind: "no-microphone" }
  return { kind: "unsupported" }
}

function release(stream: MediaStream): void {
  for (const track of stream.getTracks()) track.stop()
}

export async function startRecording(deps: VoiceDeps): Promise<RecorderHandle> {
  let stream: MediaStream
  try {
    stream = await deps.getUserMedia()
  } catch (error) {
    throw new VoiceError(failureOf(error))
  }

  const wanted = pickMimeType(deps.isTypeSupported)
  let recorder: RecorderLike
  try {
    recorder = deps.createRecorder(stream, wanted)
  } catch {
    release(stream)
    throw new VoiceError({ kind: "unsupported" })
  }

  const chunks: Blob[] = []
  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data)
  }
  const startedAt = deps.now()
  recorder.start()

  let finished = false
  return {
    stop: () =>
      new Promise<VoiceRecording>((resolve, reject) => {
        if (finished) {
          reject(new VoiceError({ kind: "unsupported" }))
          return
        }
        finished = true
        recorder.onstop = () => {
          release(stream)
          const mimeType = baseMimeType(recorder.mimeType || wanted || "audio/webm")
          resolve({
            blob: new Blob(chunks, { type: mimeType }),
            mimeType,
            durationMs: Math.max(1, Math.round(deps.now() - startedAt)),
          })
        }
        recorder.stop()
      }),
    cancel: () => {
      if (finished) return
      finished = true
      recorder.onstop = null
      try {
        recorder.stop()
      } catch {
        // Запись уже остановлена — освободить микрофон всё равно нужно.
      }
      release(stream)
    },
  }
}

/** Браузерные зависимости. Вынесены отдельно, чтобы тесты их не трогали. */
export function browserVoiceDeps(): VoiceDeps | null {
  if (
    typeof navigator === "undefined" ||
    navigator.mediaDevices?.getUserMedia === undefined ||
    typeof MediaRecorder === "undefined"
  ) {
    return null
  }
  return {
    getUserMedia: () => navigator.mediaDevices.getUserMedia({ audio: true }),
    createRecorder: (stream, mimeType) =>
      new MediaRecorder(stream, mimeType === undefined ? undefined : { mimeType }) as unknown as RecorderLike,
    isTypeSupported: (mimeType) => MediaRecorder.isTypeSupported(mimeType),
    now: () => performance.now(),
  }
}

export function voiceFailureMessage(failure: VoiceFailure): string {
  switch (failure.kind) {
    case "permission-denied":
      return "Microphone access was blocked. Allow it in the browser to record."
    case "no-microphone":
      return "No microphone found."
    case "unsupported":
      return "Voice recording isn't supported in this browser."
  }
}

/** Максимум, до которого рекордер сам останавливает запись (совпадает с сервером). */
export const MAX_RECORDING_MS = 5 * 60 * 1000
