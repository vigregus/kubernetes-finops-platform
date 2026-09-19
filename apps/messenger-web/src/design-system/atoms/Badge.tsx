interface BadgeProps {
  count: number
}

export function Badge({ count }: BadgeProps) {
  return (
    <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-accent-terracotta text-[11px] font-semibold text-on-primary shadow-sm">
      {count}
    </span>
  )
}
