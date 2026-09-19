import type { InputHTMLAttributes } from "react"
import { clsx } from "clsx"

type TextFieldProps = InputHTMLAttributes<HTMLInputElement>

export function TextField({ className, ...props }: TextFieldProps) {
  return (
    <input
      type="text"
      className={clsx(
        "w-full border-none bg-transparent text-sm text-on-surface outline-none placeholder:text-text-warm-muted",
        className,
      )}
      {...props}
    />
  )
}
