/** `m:ss` для длительности в секундах; без значения — `0:00`. */
export function formatDuration(seconds?: number): string {
  if (!seconds) return "0:00"
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, "0")}`
}
