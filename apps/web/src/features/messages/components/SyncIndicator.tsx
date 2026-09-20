export function SyncIndicator() {
  return (
    <div className="flex items-center justify-center py-2">
      <div className="inline-flex items-center gap-2 rounded-full bg-surface-container-low px-3 py-1 text-xs text-text-warm-secondary shadow-[0_1px_3px_rgba(41,37,36,0.03)]">
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent-amber opacity-75" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-accent-amber" />
        </span>
        <span>Syncing messages…</span>
      </div>
    </div>
  )
}
