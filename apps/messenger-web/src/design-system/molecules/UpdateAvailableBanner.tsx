import { Icon } from "../atoms/Icon"

interface UpdateAvailableBannerProps {
  onReloadNow: () => void
}

/**
 * 13-client-compatibility.md Часть 2/5: an incompatible Service Worker update defers
 * reload until the user isn't typing, or asks them to reload now.
 */
export function UpdateAvailableBanner({ onReloadNow }: UpdateAvailableBannerProps) {
  return (
    <div className="flex items-center justify-between gap-3 bg-text-charcoal px-4 py-2 text-sm text-on-primary">
      <div className="flex items-center gap-2">
        <Icon name="download" size={18} />
        <span>A new version of Vector is ready.</span>
      </div>
      <button
        type="button"
        onClick={onReloadNow}
        className="rounded-lg bg-white/15 px-3 py-1 text-xs font-semibold hover:bg-white/25"
      >
        Reload now
      </button>
    </div>
  )
}
