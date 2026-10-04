/**
 * Запись голоса в композере: простое состояние поверх `startRecording`.
 *
 * Готовая запись не уходит сама: она отдаётся в `onRecorded` и дальше идёт тем
 * же путём, что и выбранный файл (`useAttachmentDraft.pick`) — второго пути
 * загрузки нет, а человек перед отправкой видит черновик с длиной записи.
 */
import { useCallback, useEffect, useRef, useState } from "react"

import {
  MAX_RECORDING_MS,
  startRecording,
  VoiceError,
  voiceFailureMessage,
  type RecorderHandle,
  type VoiceDeps,
} from "./voiceRecorder"

export type VoiceState =
  | { readonly state: "idle" }
  | { readonly state: "recording"; readonly elapsedMs: number }
  | { readonly state: "failed"; readonly message: string }

export interface UseVoiceRecorder {
  readonly voice: VoiceState
  start(): void
  stop(): void
  cancel(): void
}

const TICK_MS = 250
const EXTENSION: Record<string, string> = { "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a" }

export function useVoiceRecorder(
  deps: VoiceDeps | null,
  onRecorded: (file: File, meta: { durationMs: number }) => void,
): UseVoiceRecorder {
  const [voice, setVoice] = useState<VoiceState>({ state: "idle" })
  const handle = useRef<RecorderHandle | null>(null)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const startedAt = useRef(0)
  const mounted = useRef(true)
  const onRecordedRef = useRef(onRecorded)
  useEffect(() => {
    onRecordedRef.current = onRecorded
  })

  const clearTimer = useCallback(() => {
    if (timer.current !== null) clearInterval(timer.current)
    timer.current = null
  }, [])

  const stop = useCallback(() => {
    const current = handle.current
    if (current === null) return
    handle.current = null
    clearTimer()
    void current.stop().then((recording) => {
      if (!mounted.current) return
      setVoice({ state: "idle" })
      const extension = EXTENSION[recording.mimeType] ?? "webm"
      const file = new File([recording.blob], `voice.${extension}`, { type: recording.mimeType })
      onRecordedRef.current(file, { durationMs: recording.durationMs })
    })
  }, [clearTimer])

  const cancel = useCallback(() => {
    handle.current?.cancel()
    handle.current = null
    clearTimer()
    setVoice({ state: "idle" })
  }, [clearTimer])

  const start = useCallback(() => {
    if (deps === null || handle.current !== null) return
    startedAt.current = Date.now()
    void startRecording(deps)
      .then((started) => {
        if (!mounted.current) {
          started.cancel()
          return
        }
        handle.current = started
        setVoice({ state: "recording", elapsedMs: 0 })
        timer.current = setInterval(() => {
          const elapsed = Date.now() - startedAt.current
          if (elapsed >= MAX_RECORDING_MS) {
            stop()
            return
          }
          setVoice({ state: "recording", elapsedMs: elapsed })
        }, TICK_MS)
      })
      .catch((error: unknown) => {
        if (!mounted.current) return
        const failure = error instanceof VoiceError ? error.failure : { kind: "unsupported" as const }
        setVoice({ state: "failed", message: voiceFailureMessage(failure) })
      })
  }, [deps, stop])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      handle.current?.cancel()
      handle.current = null
      if (timer.current !== null) clearInterval(timer.current)
    }
  }, [])

  return { voice, start, stop, cancel }
}
