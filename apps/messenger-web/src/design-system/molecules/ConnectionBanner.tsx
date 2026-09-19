import { Icon } from "../atoms/Icon"

interface ConnectionBannerProps {
  message: string
  onDismiss?: () => void
}

export function ConnectionBanner({ message, onDismiss }: ConnectionBannerProps) {
  return (
    <div className="flex flex-shrink-0 justify-center px-6 pb-1 pt-3">
      <div className="inline-flex items-center gap-2 rounded-full bg-surface-cream px-3.5 py-1.5 text-xs text-status-warning shadow-[0_2px_6px_rgba(41,37,36,0.05)]">
        <Icon name="sync" size={16} className="animate-spin" />
        <span className="font-medium text-text-charcoal">{message}</span>
        <button type="button" onClick={onDismiss} className="ml-1 font-semibold text-accent-terracotta hover:underline">
          Dismiss
        </button>
      </div>
    </div>
  )
}
