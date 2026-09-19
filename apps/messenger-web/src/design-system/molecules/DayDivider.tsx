interface DayDividerProps {
  label: string
}

export function DayDivider({ label }: DayDividerProps) {
  return (
    <div className="my-4 flex items-center justify-center">
      <span className="rounded-full bg-surface-container-high px-3.5 py-1 text-xs font-medium tracking-wide text-text-warm-secondary shadow-2xs">
        {label}
      </span>
    </div>
  )
}
