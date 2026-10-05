import { clsx } from "clsx"
import { useEffect, useRef, useState } from "react"

import { Avatar } from "../../shared/ui/Avatar"
import { Icon } from "../../shared/ui/Icon"
import { formatCallDuration } from "./callSummary"
import type { CallEndReason, CallPhase } from "./callState"
import { VIDEO_QUALITIES, type VideoQuality } from "./callEngine"
import { useCalls } from "./CallsProvider"

const ENDED_TEXT: Readonly<Record<CallEndReason, string>> = {
  completed: "Call ended",
  declined: "Call declined",
  missed: "No answer",
  cancelled: "Call cancelled",
  busy: "Busy",
  unavailable: "Unavailable",
  failed: "Connection lost",
  accepted_elsewhere: "Answered on another device",
  media_denied: "Microphone access is blocked",
}

const STATUS_TEXT: Readonly<Partial<Record<CallPhase, string>>> = {
  outgoing: "Calling…",
  connecting: "Connecting…",
}

const QUALITY_LABEL: Readonly<Record<VideoQuality, string>> = {
  auto: "Auto",
  low: "Low · 360p",
  medium: "Medium · 480p",
  hd: "HD · 720p",
  fhd: "Full HD · 1080p",
}

/**
 * Выбор качества **исходящего** видео. Меню, а не список всегда на экране: в
 * разговоре нужна картинка, а не настройки. Атрибут `data-call-quality` — то, чем
 * приёмка читает выбор, не разбирая слова.
 */
function VideoQualityMenu({
  value,
  onChange,
}: {
  value: VideoQuality
  onChange: (quality: VideoQuality) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="relative" data-call-quality={value}>
      <button
        type="button"
        aria-label="Video quality"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className={clsx(
          "flex h-14 w-14 items-center justify-center rounded-full transition-colors",
          open ? "bg-white text-stone-900" : "bg-white/15 text-white hover:bg-white/25",
        )}
      >
        <Icon name="hd" size={24} />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Outgoing video quality"
          className="absolute bottom-16 left-1/2 w-48 -translate-x-1/2 rounded-2xl bg-stone-800 p-1.5 text-sm shadow-xl"
        >
          <p className="px-3 pb-1 pt-1.5 text-xs text-white/60">Your outgoing video</p>
          {VIDEO_QUALITIES.map((quality) => (
            <button
              key={quality}
              type="button"
              role="menuitemradio"
              aria-checked={quality === value}
              onClick={() => {
                onChange(quality)
                setOpen(false)
              }}
              className={clsx(
                "flex w-full items-center justify-between rounded-xl px-3 py-2 text-left hover:bg-white/10",
                quality === value && "bg-white/15 font-semibold",
              )}
            >
              {QUALITY_LABEL[quality]}
              {quality === value && <Icon name="check" size={16} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** Поток к элементу — через ref: `srcObject` нельзя задать атрибутом. */
function useStream(stream: MediaStream | null) {
  const ref = useRef<HTMLVideoElement | null>(null)
  useEffect(() => {
    const element = ref.current
    if (element === null) return
    element.srcObject = stream
    if (stream !== null) void element.play().catch(() => undefined)
  }, [stream])
  return ref
}

function useElapsed(active: boolean): number {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    if (!active) return
    const started = Date.now()
    setSeconds(0)
    const id = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => window.clearInterval(id)
  }, [active])
  return seconds
}

/**
 * Экран звонка: входящий вызов и сам разговор. Монтируется один раз над
 * страницей, поэтому звонок не прерывается переключением бесед.
 *
 * Атрибуты `data-call-*` — то, чем приёмка читает состояние, не разбирая слова
 * интерфейса.
 */
export function CallOverlay() {
  const calls = useCalls()
  const remoteRef = useStream(calls?.remoteStream ?? null)
  const localRef = useStream(calls?.localStream ?? null)
  const elapsed = useElapsed(calls?.view.phase === "active")

  if (calls === null) return null
  const { view, notice } = calls

  const toast = notice !== null && (
    <div
      role="status"
      data-call-notice
      className="fixed left-1/2 top-4 z-[60] -translate-x-1/2 rounded-xl bg-on-surface px-4 py-2 text-sm text-surface shadow-lg"
    >
      {notice}
    </div>
  )

  if (view.phase === "idle" || (view.phase === "ended" && view.endReason === "accepted_elsewhere")) {
    return <>{toast}</>
  }

  const name = view.peerName ?? "Unknown"
  const isVideo = view.kind === "video"

  if (view.phase === "incoming") {
    return (
      <>
        {toast}
        <div
          role="alertdialog"
          aria-label="Incoming call"
          data-call-phase="incoming"
          data-call-kind={view.kind}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
        >
          <div className="flex w-full max-w-sm flex-col items-center gap-5 rounded-3xl bg-surface p-8 shadow-2xl">
            <Avatar name={name} size="lg" />
            <div className="text-center">
              <p className="text-xl font-semibold text-on-surface">{name}</p>
              <p className="mt-1 text-sm text-text-warm-secondary">
                Incoming {isVideo ? "video" : "voice"} call…
              </p>
            </div>
            <div className="flex items-center gap-8">
              <button
                type="button"
                aria-label="Decline call"
                onClick={calls.decline}
                className="flex h-14 w-14 items-center justify-center rounded-full bg-red-600 text-white shadow-md transition-transform active:scale-95"
              >
                <Icon name="call_end" size={26} />
              </button>
              <button
                type="button"
                aria-label="Accept call"
                onClick={calls.accept}
                className="flex h-14 w-14 items-center justify-center rounded-full bg-green-600 text-white shadow-md transition-transform active:scale-95"
              >
                <Icon name={isVideo ? "videocam" : "call"} size={26} />
              </button>
            </div>
          </div>
        </div>
      </>
    )
  }

  const ended = view.phase === "ended"
  const status = ended
    ? ENDED_TEXT[view.endReason ?? "completed"]
    : view.phase === "active"
      ? formatCallDuration(elapsed)
      : (STATUS_TEXT[view.phase] ?? "")
  const showVideo = isVideo && calls.remoteStream !== null && !ended

  return (
    <>
      {toast}
      <div
        role="dialog"
        aria-label="Call"
        data-call-phase={view.phase}
        data-call-kind={view.kind}
        data-call-reason={view.endReason ?? undefined}
        className="fixed inset-0 z-50 flex flex-col bg-stone-900 text-white"
      >
        {/* Удалённый звук идёт через этот элемент и в аудиозвонке: скрытый, но играющий. */}
        <video
          ref={remoteRef}
          autoPlay
          playsInline
          data-call-remote
          className={clsx(
            "absolute inset-0 h-full w-full object-cover",
            !showVideo && "invisible",
          )}
        />
        {isVideo && calls.localStream !== null && !ended && (
          <video
            ref={localRef}
            autoPlay
            playsInline
            muted
            data-call-local
            className={clsx(
              "absolute right-4 top-4 z-10 h-36 w-24 rounded-xl border border-white/20 object-cover shadow-lg sm:h-44 sm:w-32",
              view.cameraOff && "invisible",
            )}
          />
        )}

        <div className="relative z-10 flex flex-1 flex-col items-center justify-center gap-4 p-6">
          {!showVideo && <Avatar name={name} size="lg" />}
          <div className="text-center drop-shadow">
            <p className="text-2xl font-semibold">{name}</p>
            <p data-call-status className="mt-1 text-sm text-white/80">
              {status}
            </p>
          </div>
        </div>

        {!ended && (
          <div className="relative z-10 flex items-center justify-center gap-5 p-8 pb-10">
            <button
              type="button"
              aria-label={view.muted ? "Unmute microphone" : "Mute microphone"}
              aria-pressed={view.muted}
              onClick={() => calls.setMuted(!view.muted)}
              className={clsx(
                "flex h-14 w-14 items-center justify-center rounded-full transition-colors",
                view.muted ? "bg-white text-stone-900" : "bg-white/15 text-white hover:bg-white/25",
              )}
            >
              <Icon name={view.muted ? "mic_off" : "mic"} size={24} />
            </button>
            {isVideo && (
              <button
                type="button"
                aria-label={view.cameraOff ? "Turn camera on" : "Turn camera off"}
                aria-pressed={view.cameraOff}
                onClick={() => calls.setCameraOff(!view.cameraOff)}
                className={clsx(
                  "flex h-14 w-14 items-center justify-center rounded-full transition-colors",
                  view.cameraOff ? "bg-white text-stone-900" : "bg-white/15 text-white hover:bg-white/25",
                )}
              >
                <Icon name={view.cameraOff ? "videocam_off" : "videocam"} size={24} />
              </button>
            )}
            {isVideo && <VideoQualityMenu value={calls.videoQuality} onChange={calls.setVideoQuality} />}
            <button
              type="button"
              aria-label="End call"
              onClick={calls.hangup}
              className="flex h-14 w-14 items-center justify-center rounded-full bg-red-600 text-white shadow-md transition-transform active:scale-95"
            >
              <Icon name="call_end" size={26} />
            </button>
          </div>
        )}
      </div>
    </>
  )
}
