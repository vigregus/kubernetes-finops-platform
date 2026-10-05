import { clsx } from "clsx"
import { Icon } from "./Icon"

interface IconButtonProps {
  icon: string
  label: string
  onClick?: () => void
  variant?: "ghost" | "filled"
  className?: string
  /**
   * Недоступна, но **нажимаема**: `aria-disabled`, а не `disabled`. Выключенная кнопка не
   * получает ни фокуса, ни касания, и причину («почему серая») на телефоне, где нет
   * наведения, показать некому. Что делает нажатие — решает вызывающий (например, показать причину).
   */
  unavailable?: boolean
  /** Элемент с описанием причины (`aria-describedby`). */
  describedBy?: string
}

export function IconButton({ icon, label, onClick, variant = "ghost", className, unavailable, describedBy }: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      aria-disabled={unavailable ? true : undefined}
      aria-describedby={describedBy}
      className={clsx(
        "flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl transition-colors",
        variant === "ghost" &&
          "text-text-warm-secondary hover:bg-surface-container-low hover:text-on-surface",
        variant === "filled" &&
          "bg-surface-cream text-on-surface shadow-[0_2px_6px_rgba(41,37,36,0.04)] hover:bg-surface-warm-subtle active:scale-95",
        unavailable && "opacity-40 hover:bg-transparent hover:text-text-warm-secondary",
        className,
      )}
    >
      <Icon name={icon} size={20} />
    </button>
  )
}
