import * as RadixAvatar from "@radix-ui/react-avatar"
import { clsx } from "clsx"
import type { PresenceStatus } from "../lib/types"
import { StatusDot } from "./StatusDot"

interface AvatarProps {
  name: string
  src?: string
  initials?: string
  size?: "sm" | "md" | "lg"
  presence?: PresenceStatus
  ringClassName?: string
}

const sizeClass = {
  sm: "h-8 w-8 text-xs",
  md: "h-10 w-10 text-sm",
  lg: "h-11 w-11 text-sm",
}

function fallbackInitials(name: string) {
  return name
    .split(" ")
    .map((part) => part[0])
    .slice(0, 2)
    .join("")
    .toUpperCase()
}

export function Avatar({ name, src, initials, size = "md", presence, ringClassName }: AvatarProps) {
  return (
    <div className="relative flex-shrink-0">
      <RadixAvatar.Root
        className={clsx(
          "flex items-center justify-center overflow-hidden rounded-full bg-surface-container-high font-semibold text-on-surface",
          sizeClass[size],
        )}
      >
        <RadixAvatar.Image src={src} alt={name} className="h-full w-full object-cover" />
        <RadixAvatar.Fallback delayMs={src ? 400 : 0}>{initials ?? fallbackInitials(name)}</RadixAvatar.Fallback>
      </RadixAvatar.Root>
      {presence && <StatusDot presence={presence} ringClassName={ringClassName} />}
    </div>
  )
}
