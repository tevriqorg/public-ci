import type { ButtonHTMLAttributes } from "react"
import { cn } from "../../lib/utils"

export function Button({ className, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={cn("inline-flex h-9 items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/7 px-3 text-sm font-medium text-slate-100 transition hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-40", className)} {...props} />
}
