export function Progress({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0))
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-white/8">
      <div className="h-full rounded-full bg-cyan-400 transition-[width] duration-500" style={{ width: `${pct}%` }} />
    </div>
  )
}
