interface TypingIndicatorProps {
  /** RT-004: "Greg печатает" / "Greg и Maria печатают" / "печатают трое" */
  names: string[]
}

function label(names: string[]) {
  if (names.length === 1) return `${names[0]} is typing`
  if (names.length === 2) return `${names[0]} and ${names[1]} are typing`
  return `${names.length} people are typing`
}

export function TypingIndicator({ names }: TypingIndicatorProps) {
  if (names.length === 0) return null
  return (
    <div className="flex items-center gap-2 px-1 text-xs text-text-warm-muted">
      <span className="flex items-center gap-0.5">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-text-warm-muted [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-text-warm-muted [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-text-warm-muted" />
      </span>
      {label(names)}
    </div>
  )
}
