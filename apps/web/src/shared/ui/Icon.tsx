import { clsx } from "clsx"

interface IconProps {
  name: string
  className?: string
  size?: number
}

export function Icon({ name, className, size = 20 }: IconProps) {
  return (
    <span
      className={clsx("material-symbol select-none", className)}
      style={{ fontSize: size }}
      aria-hidden="true"
    >
      {name}
    </span>
  )
}
