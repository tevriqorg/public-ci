import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Activity, ArrowDownToLine, ArrowUpFromLine, Check, CircleAlert, Copy,
  Database, HardDrive, Link2, LoaderCircle, LockKeyhole,
  MoreHorizontal, Network, Plus, RefreshCcw, RotateCcw, Server,
  Pause, Play, Square, X, Folder, File, ChevronLeft, PackageOpen, Clock3, ListChecks,
} from "lucide-react"
import { api } from "./api"
import type { Health, Job, JobEvent, JobState, LocalFileEntry, WatcherRun, WatcherTask } from "./types"
import { receiveInBrowser } from "./browser_receiver"
import type { ReceiverDebugEntry, ReceiverPhase, ReceiverProgressEvent } from "./browser_receiver"
import { Badge } from "./components/ui/badge"
import { Button } from "./components/ui/button"
import { Card } from "./components/ui/card"
import { Progress } from "./components/ui/progress"
import { formatBytes, formatEta, formatSpeed, formatTime } from "./lib/utils"

async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {}
  const textarea=document.createElement("textarea")
  textarea.value=value
  textarea.style.position="fixed"
  textarea.style.opacity="0"
  document.body.appendChild(textarea)
  textarea.focus(); textarea.select()
  let ok=false
  try { ok=document.execCommand("copy") } catch {}
  textarea.remove()
  return ok
}

const activeStates = new Set<JobState>([
  "QUEUED", "FETCHING", "VERIFYING", "PACKING", "ENCRYPTING",
  "FLASH_UPLOADING", "PAUSED", "CLEANUP",
])

const stateLabel: Record<JobState, string> = {
  QUEUED: "排队", FETCHING: "下载中", VERIFYING: "校验中", PACKING: "打包中",
  ENCRYPTING: "加密中", FLASH_UPLOADING: "Flash 上传", READY: "可取回",
  RETRIEVED: "已取回", DOWNLOADED: "已取回（旧）", CLEANUP: "清理中", DONE: "完成", FAILED: "失败",
  CANCELLED: "已取消", PAUSED: "已暂停",
}

function tone(state: JobState) {
  if (state === "FAILED") return "border-rose-400/20 bg-rose-400/10 text-rose-200"
  if (state === "READY") return "border-emerald-400/20 bg-emerald-400/10 text-emerald-200"
  if (state === "DONE") return "border-white/8 bg-white/5 text-slate-400"
  if (state === "CANCELLED") return "border-amber-400/20 bg-amber-400/10 text-amber-200"
  return "border-cyan-400/20 bg-cyan-400/10 text-cyan-200"
}

function StatCard({ icon, label, value, sub }: { icon: ReactNode; label: string; value: string; sub?: string }) {
  return <Card className="p-4">
    <div className="mb-3 flex items-center justify-between text-slate-500"><span className="text-xs uppercase tracking-[.14em]">{label}</span>{icon}</div>
    <div className="text-xl font-semibold tracking-tight text-slate-100">{value}</div>
    {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
  </Card>
}

function JobRow({ job, onSelect }: { job: Job; onSelect: (job: Job) => void }) {
  const shownProgress = job.state === "FLASH_UPLOADING" ? job.upload_progress : job.progress
  return <button onClick={()=>onSelect(job)}
    className="grid w-full grid-cols-[minmax(0,2.2fr)_130px_1fr_110px_120px_28px] items-center gap-4 border-t border-white/6 px-4 py-4 text-left transition hover:bg-white/[.025] max-lg:grid-cols-[minmax(0,1fr)_110px_28px]">
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[11px] text-cyan-300">{job.job_number_label}</span><span className="min-w-0"><span className="block truncate font-medium text-slate-200">{job.title || job.filename}</span>{job.public_name_preview&&<span className="block truncate font-mono text-[10px] text-slate-600">对外：{job.public_name_preview}</span>}</span>
        {job.options.scheduled&&<Badge className="border-violet-400/20 bg-violet-400/10 text-violet-200">定时任务</Badge>}
        <Badge className={tone(job.state)}>{stateLabel[job.state]}</Badge>
      </div>
      <div className="mt-2"><Progress value={shownProgress || 0}/></div>
      <div className="mt-1.5 flex gap-3 text-xs text-slate-500">
        <span>{(shownProgress || 0).toFixed(0)}%</span><span>{job.engine}</span><span>{job.current_stage || job.source_type}</span>{job.queue_position&&<span>队列 #{job.queue_position}</span>}
      </div>
    </div>
    <div className="max-lg:hidden"><div className="text-sm text-slate-300">{formatSpeed(job.download_speed)}</div><div className="text-xs text-slate-600">下载</div></div>
    <div className="max-lg:hidden"><div className="text-sm text-slate-300">{formatSpeed(job.upload_speed)}</div><div className="text-xs text-slate-600">上传</div></div>
    <div className="max-lg:hidden"><div className="text-sm text-slate-300">{formatEta(job.eta_seconds)}</div><div className="text-xs text-slate-600">ETA</div></div>
    <div className="max-lg:text-right"><div className="truncate text-sm text-slate-300">{job.options.delivery_backend || "—"}</div><div className="text-xs text-slate-600">{formatTime(job.created_at)}</div></div>
    <MoreHorizontal size={17} className="text-slate-600"/>
  </button>
}

const receiverPhaseLabel: Record<ReceiverPhase, string> = {
  RESOLVING: "解析接收地址",
  DOWNLOADING: "下载",
  DECRYPTING_EXTRACTING: "解密解压",
  SAVING: "保存",
  WAITING_ACK: "等待确认",
  DONE: "完成",
}

function ReceiverProgressPanel({ progress }: { progress: ReceiverProgressEvent }) {
  const downloadDone = progress.download_bytes_done ?? progress.bytes_done
  const downloadTotal = progress.download_bytes_total ?? progress.bytes_total
  const downloadPercent = progress.download_percent ?? (downloadDone !== undefined && downloadTotal ? (downloadDone / downloadTotal) * 100 : undefined)
  const extracting = progress.phase === "DECRYPTING_EXTRACTING"
  return <div className="mt-3 min-w-0 rounded-lg border border-cyan-400/10 bg-black/15 p-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="text-xs font-medium text-cyan-200">Receiver · {receiverPhaseLabel[progress.phase]}</div>
      {progress.phase === "DONE" && <Badge className="border-emerald-400/20 bg-emerald-400/10 text-emerald-200">完成</Badge>}
    </div>
    {downloadTotal !== undefined && downloadTotal > 0 && <>
      <div className="mt-3 flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px] text-slate-400">
        <span>QQ CDN 下载</span>
        <span className="font-mono">{formatBytes(downloadDone || 0)} / {formatBytes(downloadTotal)}</span>
      </div>
      <div className="mt-1.5"><Progress value={downloadPercent || 0}/></div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
        {downloadPercent !== undefined && <span>{downloadPercent.toFixed(1)}%</span>}
        {progress.speed_bps !== undefined && progress.speed_bps > 0 && <span>{formatSpeed(progress.speed_bps)}</span>}
        {progress.eta_seconds !== undefined && progress.eta_seconds > 0 && <span>ETA {formatEta(progress.eta_seconds)}</span>}
      </div>
    </>}
    {extracting && <>
      <div className="mt-3 min-w-0 text-[11px] text-slate-400">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>解密 / 解压</span>
          {progress.entry_index && progress.entry_count && <span>第 {progress.entry_index} / {progress.entry_count} 个</span>}
        </div>
        {progress.artifact_name && <div className="mt-1 break-all font-mono text-slate-300">{progress.artifact_name}</div>}
      </div>
      {progress.entry_bytes_total !== undefined && progress.entry_bytes_total > 0 && <>
        <div className="mt-2"><Progress value={progress.percent || 0}/></div>
        <div className="mt-1.5 flex flex-wrap justify-between gap-2 font-mono text-[10px] text-slate-500">
          <span>{formatBytes(progress.entry_bytes_done || 0)} / {formatBytes(progress.entry_bytes_total)}</span>
          <span>{(progress.percent || 0).toFixed(1)}%</span>
        </div>
      </>}
    </>}
  </div>
}

function Detail({ job, events, onClose, onAction, onClear }: {
  job: Job
  events: JobEvent[]
  onClose: () => void
  onAction: (kind: "pause"|"resume"|"cancel"|"retry"|"ack", id: string) => Promise<void>
  onClear: (id:string) => Promise<void>
}) {
  const item = job.result?.items?.[0]
  const publicNames = job.options.public_names?.length ? job.options.public_names : (job.result?.items || []).map(x=>x.filename).filter(Boolean) as string[]
  const publicNamePreview = publicNames[0] || job.public_name_preview || ""
  const shownProgress = job.state === "FLASH_UPLOADING" ? job.upload_progress : job.progress
  const [busy,setBusy] = useState(false)
  const [copyFeedback,setCopyFeedback] = useState("")
  const [receiverStatus,setReceiverStatus] = useState("")
  const [receiverProgress,setReceiverProgress] = useState<ReceiverProgressEvent | null>(null)
  const [receiverError,setReceiverError] = useState("")
  const [receiverDebug,setReceiverDebug] = useState<ReceiverDebugEntry[]>([])
  const [receiverDebugOpen,setReceiverDebugOpen] = useState(false)
  const pushReceiverDebug = (entry: ReceiverDebugEntry) => setReceiverDebug(prev=>[...prev.slice(-79),entry])
  const act = async (kind:"pause"|"resume"|"cancel"|"retry"|"ack") => {
    setBusy(true); try { await onAction(kind, job.id) } finally { setBusy(false) }
  }
  const copy = async (label:string,value?:string|null) => {
    if (!value) return
    const ok=await copyText(value)
    setCopyFeedback(ok?`${label}已复制`:"复制失败，请手动选择内容")
    window.setTimeout(()=>setCopyFeedback(""),ok?1800:3000)
  }
  const browserReceive = async () => {
    const preparing: ReceiverProgressEvent = { phase:"RESOLVING", message:"正在准备 Receiver ticket…" }
    setBusy(true); setReceiverError(""); setReceiverStatus(preparing.message); setReceiverProgress(preparing)
    setReceiverDebug([]); setReceiverDebugOpen(false)
    pushReceiverDebug({ at:new Date().toISOString(), phase:"ticket", message:"请求 Receiver ticket" })
    try {
      const ticket=await api.receiverTicket(job.id)
      pushReceiverDebug({ at:new Date().toISOString(), phase:"ticket", message:"Receiver ticket 获取成功", data:{ artifacts:ticket.artifacts.length, postprocess:ticket.postprocess?.kind || "none" } })
      const result=await receiveInBrowser(ticket,(progress)=>{ setReceiverProgress(progress); setReceiverStatus(progress.message) },pushReceiverDebug)
      if (result.extracted) {
        const waitingMessage=`已解密解压 ${result.extracted} 个文件。任务仍保留，可再次接收；确认文件无误后请点击“已取回，清理”。`
        setReceiverStatus(waitingMessage)
        setReceiverProgress(prev=>prev ? { ...prev, phase:"WAITING_ACK", message:waitingMessage, eta_seconds:0 } : { phase:"WAITING_ACK", message:waitingMessage, percent:100 })
      } else {
        setReceiverStatus("下载已交给浏览器；任务仍保留，可再次接收。确认文件保存完成后请点击“已取回，清理”。")
      }
    } catch(e) {
      const message=e instanceof Error?e.message:"浏览器接收失败"
      pushReceiverDebug({ at:new Date().toISOString(), phase:"error", message:"浏览器接收失败", data:{ error:message } })
      setReceiverError(message); setReceiverDebugOpen(true)
    }
    finally { setBusy(false) }
  }
  return <div className="fixed inset-0 z-40 flex justify-end bg-black/45 backdrop-blur-sm" onMouseDown={(e)=>e.currentTarget===e.target&&onClose()}>
    <aside className="h-full w-full max-w-xl overflow-y-auto border-l border-white/10 bg-[#0b1422] p-6 shadow-2xl">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="min-w-0"><Badge className={tone(job.state)}>{stateLabel[job.state]}</Badge><h2 className="mt-3 truncate text-xl font-semibold">{job.job_number_label} · {job.title || job.filename}</h2><p className="mt-1 font-mono text-xs text-slate-600">{job.id}</p></div>
        <Button onClick={onClose} className="size-9 px-0"><X size={16}/></Button>
      </div>
      {(item?.share_link || job.archive_password || publicNamePreview || job.state==="READY") && <Card className="mb-4 border-emerald-400/15 bg-emerald-400/[.04] p-4">
        <div className="mb-3 flex items-center justify-between gap-3"><div className="text-sm font-medium text-slate-200">交付与取回</div>{job.state==="READY"&&<Badge className="border-emerald-400/20 bg-emerald-400/10 text-emerald-200">Receiver Ready</Badge>}</div>
        {job.options.delivery_profile && ["iphone","android"].includes(job.options.delivery_profile) && <p className="mb-3 rounded-lg bg-amber-400/10 p-3 text-sm text-amber-200">这是统一加密策略启用前创建的历史移动任务，仍按原任务语义提供未加密文件。</p>}
        {job.state==="READY"&&job.options.delivery_backend==="qq_flash"&&<Button disabled={busy} onClick={browserReceive} className="h-11 w-full bg-emerald-400 text-slate-950 hover:bg-emerald-300"><PackageOpen size={16}/>{job.options.pack?"浏览器接收并解密解压":"浏览器下载"}</Button>}
        {job.state==="READY"&&job.options.pack&&<p className="mt-2 text-xs leading-5 text-slate-500">统一安全交付：AES-256 ZIP 在浏览器端解密。完成后任务仍保持 READY，可再次接收；只有点击“已取回，清理”才会清理 Tokyo 工作副本。</p>}
        {receiverProgress&&<ReceiverProgressPanel progress={receiverProgress}/>}
        {receiverStatus&&<p role="status" className="mt-3 rounded-lg bg-cyan-400/8 p-3 text-xs leading-5 text-cyan-200">{receiverStatus}</p>}
        {receiverError&&<p role="alert" className="mt-3 rounded-lg bg-rose-400/8 p-3 text-xs text-rose-200">{receiverError}</p>}
        {receiverDebug.length>0&&<details open={receiverDebugOpen} onToggle={e=>setReceiverDebugOpen(e.currentTarget.open)} className="mt-3 rounded-lg border border-white/8 bg-black/15 p-3">
          <summary className="cursor-pointer text-xs font-medium text-slate-400">接收诊断 · {receiverDebug.length} 条</summary>
          <div className="mt-3 flex items-center justify-between gap-2"><span className="text-[11px] text-slate-600">不会记录 CDN 签名 URL、解压密码或 API token</span><Button onClick={()=>copy("诊断日志",JSON.stringify({job:job.job_number_label,job_id:job.id,collected_at:new Date().toISOString(),entries:receiverDebug},null,2))}><Copy size={13}/>复制诊断</Button></div>
          <div className="mt-3 max-h-72 space-y-2 overflow-y-auto font-mono text-[10px] leading-4">
            {receiverDebug.map((entry,index)=><div key={`${entry.at}-${index}`} className={`rounded-md p-2 ${entry.phase==="error"?"bg-rose-400/8 text-rose-200":"bg-black/20 text-slate-400"}`}><div><span className="text-cyan-300">[{entry.phase}]</span> {entry.message}</div>{entry.data&&<div className="mt-1 break-all text-slate-500">{JSON.stringify(entry.data)}</div>}</div>)}
          </div>
        </details>}
        {publicNamePreview&&<div className="mt-3 rounded-lg border border-cyan-400/10 bg-cyan-400/8 p-3 text-sm"><div className="text-xs text-slate-500">{publicNames.length ? "对外文件名" : "预计对外文件名"}</div>{(publicNames.length ? publicNames : [publicNamePreview]).map(name=><div key={name} className="mt-1 break-all font-mono text-cyan-200">{name}</div>)}</div>}
        {(item?.share_link || job.archive_password)&&<details className="mt-3 rounded-lg border border-white/8 bg-black/10 p-3 text-sm"><summary className="cursor-pointer text-slate-400">高级 / 手动接收方式</summary>
          {item?.share_link&&<div className="mt-3 flex flex-wrap items-center gap-2"><a href={item.share_link} target="_blank" rel="noreferrer" className="min-w-0 flex-1 break-all text-sm text-emerald-200 hover:underline"><Link2 className="mr-1 inline" size={15}/>QQ Flash 分享链接 ↗</a><Button onClick={()=>copy("链接",item.share_link)}><Copy size={14}/>复制链接</Button></div>}
          {job.archive_password&&<div className="mt-3 flex flex-wrap items-center gap-2"><code className="min-w-0 flex-1 break-all text-sm text-amber-200">解压密码：{job.archive_password}</code><Button onClick={()=>copy("密码",job.archive_password)}><Copy size={14}/>复制密码</Button></div>}
        </details>}
        {copyFeedback&&<p role="status" className="mt-3 text-xs text-emerald-300">{copyFeedback}</p>}
      </Card>}
      {job.error && <Card className="mb-4 border-rose-400/15 bg-rose-400/7 p-4 text-sm text-rose-200"><div className="mb-2 flex items-center gap-2 font-medium"><CircleAlert size={16}/>任务失败</div><div className="break-words text-rose-200/80">{job.error}</div></Card>}
      <div className="mb-4 flex flex-wrap gap-2">
        {job.state==="FETCHING" && job.engine==="aria2" && <Button disabled={busy} onClick={()=>act("pause")}><Pause size={15}/>暂停</Button>}
        {job.state==="PAUSED" && job.engine==="aria2" && <Button disabled={busy} onClick={()=>act("resume")}><Play size={15}/>继续</Button>}
        {activeStates.has(job.state)&&<Button disabled={busy} onClick={()=>act("cancel")}><Square size={15}/>取消</Button>}
        {job.state==="FAILED"&&<Button disabled={busy} onClick={()=>act("retry")}><RotateCcw size={15}/>重试</Button>}
        {job.state==="READY"&&<Button disabled={busy} onClick={()=>act("ack")} className="bg-emerald-400 text-slate-950 hover:bg-emerald-300"><Check size={15}/>已取回，清理</Button>}
        {["DONE","FAILED","CANCELLED"].includes(job.state)&&<Button disabled={busy} onClick={async()=>{setBusy(true);try{await onClear(job.id);onClose()}finally{setBusy(false)}}}><CircleAlert size={15}/>清除此历史</Button>}
      </div>
      {(job.current_stage || job.waiting_reason) && <Card className="mb-4 p-4"><div className="flex flex-wrap items-center gap-2 text-sm"><Badge>{job.current_stage || "—"}</Badge>{job.stage_state&&<span className="text-slate-300">{job.stage_state}</span>}{job.queue_position&&<span className="text-amber-200">队列位置 #{job.queue_position}</span>}</div>{job.waiting_reason&&<p className="mt-2 text-xs text-slate-500">{job.waiting_reason}</p>}</Card>}
      <Card className="mb-4 p-4"><div className="mb-2 text-xs text-slate-500">任务进度</div><Progress value={shownProgress}/><div className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div><span className="text-slate-500">阶段进度</span><div className="mt-1">{shownProgress.toFixed(1)}%</div></div>
        <div><span className="text-slate-500">下载</span><div className="mt-1">{formatSpeed(job.download_speed)}</div></div>
        <div><span className="text-slate-500">上传</span><div className="mt-1">{formatSpeed(job.upload_speed)}</div></div>
        <div><span className="text-slate-500">ETA</span><div className="mt-1">{formatEta(job.eta_seconds)}</div></div>
      </div></Card>
      <div className="space-y-2 text-sm">
        {[
          ["来源类型",job.source_type],["Downloader",job.engine],["原始大小",formatBytes(job.original_size)],
          ["最终大小",formatBytes(job.final_size)],["投递方式",job.options.delivery_backend || "—"],
          ["打包格式",job.options.pack || job.options.encrypt ? (job.options.pack_format || "7z") : "无"],
          ["内容加密",job.options.encrypt ? (job.options.pack_format === "zip" ? "ZipCrypto（兼容）" : "AES-256") : "无"],
          ["分卷大小",job.options.volume_size || "无"],
          ["VPS 本地文件",job.options.local_display_path || "无"],
          ["对外文件名（伪装名）",publicNames.join(" · ") || publicNamePreview || "尚未生成"],
          ["认证引用",job.options.auth_ref || "无"],
          ["创建时间",formatTime(job.created_at)],["完成时间",formatTime(job.completed_at)],
          ["自动清理",formatTime(job.cleanup_at)],["重试次数",String(job.retries)],
          ["匹配 Skill",job.options.matched_skill_id || job.options.applied_skill_id || "无"],
          ["失败指纹",job.options.failure_fingerprint || "无"],
        ].map(([k,v])=><div key={k} className="flex justify-between gap-4 border-b border-white/6 py-3"><span className="text-slate-500">{k}</span><span className="text-right text-slate-300">{v}</span></div>)}
      </div>
      <Card className="mt-4 p-4">
        <div className="mb-2 text-xs text-slate-500">来源</div>
        {job.options.local_display_path
          ? <span className="block break-all text-sm text-cyan-200">VPS 文件区 / {job.options.local_display_path}</span>
          : job.source && /^https?:\/\//.test(job.source)
            ? <a href={job.source} target="_blank" rel="noreferrer" className="block break-all text-sm text-cyan-200 hover:underline">{job.source}</a>
            : <span className="text-sm text-slate-300">{job.source || "历史兼容任务：无 URL 来源"}</span>}
      </Card>
      {job.sha256 && <Card className="mt-4 p-4"><div className="mb-2 flex items-center justify-between text-xs text-slate-500"><span>SHA-256</span><button onClick={()=>copy("SHA-256",job.sha256)}><Copy size={14}/></button></div><code className="break-all text-xs text-slate-300">{job.sha256}</code></Card>}
      {events.some(event=>event.type==="job.download_message") && <Card className="mt-4 p-4">
        <div className="mb-3 text-xs text-slate-500">下载过程</div>
        <div role="log" aria-live="polite" className="max-h-56 space-y-2 overflow-y-auto text-sm text-slate-300">
          {events.filter(event=>event.type==="job.download_message").slice(-20).map(event=><div key={event.id}><span className="mr-2 text-xs text-slate-500">{formatTime(event.created_at)}</span>{String(event.data.message || "")}</div>)}
        </div>
      </Card>}
      <Card className="mt-4 p-4">
        <div className="mb-3 text-xs text-slate-500">最近事件</div>
        <div className="max-h-64 space-y-2 overflow-y-auto">
          {events.length ? events.slice(-16).map((event)=><div key={event.id} className="rounded-lg bg-black/15 px-3 py-2 text-xs">
            <div className="flex justify-between gap-3"><span className="font-mono text-slate-300">{event.type}</span><span className="text-slate-600">{formatTime(event.created_at)}</span></div>
            <div className="mt-1 break-all text-slate-500">{JSON.stringify(event.data)}</div>
          </div>) : <div className="text-xs text-slate-600">暂无事件</div>}
        </div>
      </Card>
    </aside>
  </div>
}

function NewTransfer({ onClose, onCreated }: { onClose:()=>void; onCreated:(job:Job)=>void }) {
  const [mode,setMode]=useState<"url"|"vps">("url")
  const [source,setSource]=useState("")
  const [title,setTitle]=useState("")
  const [authRef,setAuthRef]=useState("")
  const [volumeSize,setVolumeSize]=useState("")
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState("")
  const [localDir,setLocalDir]=useState("")
  const [localParent,setLocalParent]=useState<string|null>(null)
  const [localEntries,setLocalEntries]=useState<LocalFileEntry[]>([])
  const [localPath,setLocalPath]=useState("")
  const [localLoading,setLocalLoading]=useState(false)

  const loadLocal=useCallback(async(path:string)=>{
    setLocalLoading(true); setError("")
    try { const listing=await api.localFiles(path); setLocalDir(listing.path); setLocalParent(listing.parent); setLocalEntries(listing.entries) }
    catch(e){ setError(e instanceof Error?e.message:"读取 VPS 文件失败") }
    finally{setLocalLoading(false)}
  },[])
  useEffect(()=>{if(mode==="vps")void loadLocal("")},[mode,loadLocal])

  async function submit(e:FormEvent) {
    e.preventDefault(); setBusy(true); setError("")
    try {
      const common={
        title:title||undefined,
        volume_size:volumeSize||undefined,
        delivery_backend:"qq_flash",
        delivery_profile:"unified" as const,
        ack_policy:"required",
      }
      const job=mode==="vps"
        ? await api.createLocalFile({path:localPath,...common})
        : await api.create({source,source_type:"auto",resolver:"auto",engine:"auto",auth_ref:authRef||undefined,...common})
      onCreated(job); onClose()
    } catch(e) { setError(e instanceof Error ? e.message : "创建失败") }
    finally { setBusy(false) }
  }
  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/55 p-5 backdrop-blur-sm" onMouseDown={(e)=>e.currentTarget===e.target&&onClose()}>
    <Card className="w-full max-w-xl bg-[#0b1422] p-5">
      <div className="mb-5 flex items-center justify-between"><div><h2 className="font-semibold">新建传输</h2><p className="mt-1 text-sm text-slate-500">下载链接或 Tokyo VPS 本地文件，后续统一走处理与 QQ Flash 投递。</p></div><Button className="size-9 px-0" onClick={onClose}><X size={16}/></Button></div>
      <div className="mb-4 grid grid-cols-2 gap-2 rounded-xl bg-black/15 p-1">
        <button type="button" onClick={()=>setMode("url")} className={`rounded-lg px-3 py-2 text-sm ${mode==="url"?"bg-cyan-400/15 text-cyan-200":"text-slate-500"}`}>网址 / 下载命令</button>
        <button type="button" onClick={()=>setMode("vps")} className={`rounded-lg px-3 py-2 text-sm ${mode==="vps"?"bg-cyan-400/15 text-cyan-200":"text-slate-500"}`}>VPS 本地文件</button>
      </div>
      <form onSubmit={submit} className="space-y-4">
        <div className="rounded-xl border border-emerald-400/15 bg-emerald-400/5 p-3"><div className="flex items-center gap-2 text-sm text-emerald-100"><LockKeyhole size={16}/>统一安全交付</div><div className="mt-1 text-xs leading-5 text-slate-400">所有新任务统一打包为 AES-256 加密 ZIP，并由 Browser Receiver 在接收端解密；不再区分桌面、iPhone/iPad 或 Android。</div></div>
        {mode==="url" ? <>
          <div className="rounded-xl border border-cyan-400/10 bg-cyan-400/5 px-3 py-2 text-sm text-cyan-100">自动识别链接类型与下载器</div>
          <textarea required rows={3} value={source} onChange={e=>setSource(e.target.value)} placeholder="粘贴网址或完整下载命令…" className="w-full resize-y rounded-xl border border-white/10 bg-black/20 px-3 py-2 outline-none focus:border-cyan-400/40"/>
          <input value={authRef} onChange={e=>setAuthRef(e.target.value)} placeholder="认证引用 auth_ref（可选）" className="h-11 w-full rounded-xl border border-white/10 bg-black/20 px-3 outline-none focus:border-cyan-400/40"/>
        </> : <div className="rounded-xl border border-white/8 bg-black/15 p-3">
          <div className="mb-2 flex items-center justify-between gap-2"><div className="min-w-0 text-xs text-slate-400">VPS 文件区 / {localDir||""}</div>{localParent!==null&&<Button type="button" onClick={()=>loadLocal(localParent||"")}><ChevronLeft size={14}/>上一级</Button>}</div>
          <div className="max-h-60 space-y-1 overflow-y-auto">
            {localLoading ? <div className="grid h-24 place-items-center text-slate-500"><LoaderCircle className="animate-spin" size={18}/></div> : localEntries.length ? localEntries.map(entry=><button type="button" key={entry.path} onClick={()=>entry.type==="directory"?loadLocal(entry.path):setLocalPath(entry.path)} className={`flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm ${localPath===entry.path?"bg-cyan-400/12 text-cyan-100":"hover:bg-white/5 text-slate-300"}`}><span className="flex min-w-0 items-center gap-2">{entry.type==="directory"?<Folder size={15}/>:<File size={15}/>}<span className="truncate">{entry.name}</span></span><span className="shrink-0 text-xs text-slate-600">{entry.type==="file"?formatBytes(entry.size):"目录"}</span></button>) : <div className="p-4 text-center text-xs text-slate-600">这个目录为空。把文件放到 /home/ubuntu/p20-transfer/local-files 后再打开或切换此页。</div>}
          </div>
          {localPath&&<div className="mt-3 rounded-lg bg-emerald-400/8 px-3 py-2 text-xs text-emerald-200">已选择：{localPath}</div>}
        </div>}
        <input value={title} onChange={e=>setTitle(e.target.value)} placeholder="标题（可选）" className="h-11 w-full rounded-xl border border-white/10 bg-black/20 px-3 outline-none focus:border-cyan-400/40"/>
        <input value={volumeSize} onChange={e=>setVolumeSize(e.target.value)} placeholder="分卷大小（可选，例如 2G）" className="h-11 w-full rounded-xl border border-white/10 bg-black/20 px-3 outline-none focus:border-cyan-400/40"/>
        {error&&<div className="text-sm text-rose-300">{error}</div>}
        <Button disabled={busy || (mode==="url"?!source:!localPath)} className="h-11 w-full bg-cyan-400 text-slate-950 hover:bg-cyan-300">{busy?<LoaderCircle className="animate-spin" size={16}/>:<Plus size={16}/>}创建任务</Button>
      </form>
    </Card>
  </div>
}


const watcherOutcomeLabel: Record<string,string> = {
  RUNNING:"执行中", CREATED:"成功 · 已创建传输", SKIPPED:"跳过 · 无更新",
  BOOTSTRAPPED:"已建立基线", FAILED:"失败",
}

function watcherOutcomeTone(outcome?: string|null) {
  if (outcome === "FAILED") return "border-rose-400/20 bg-rose-400/10 text-rose-200"
  if (outcome === "CREATED") return "border-emerald-400/20 bg-emerald-400/10 text-emerald-200"
  if (outcome === "BOOTSTRAPPED") return "border-cyan-400/20 bg-cyan-400/10 text-cyan-200"
  if (outcome === "RUNNING") return "border-amber-400/20 bg-amber-400/10 text-amber-200"
  return "border-white/8 bg-white/5 text-slate-400"
}

function intervalLabel(seconds:number) {
  if(seconds%3600===0)return `每 ${seconds/3600} 小时`
  if(seconds%60===0)return `每 ${seconds/60} 分钟`
  return `每 ${seconds} 秒`
}

function WatcherPanel({watchers,runs,jobs,onSelectJob}:{watchers:WatcherTask[];runs:WatcherRun[];jobs:Job[];onSelectJob:(job:Job)=>void}) {
  const byId=useMemo(()=>new Map(watchers.map(w=>[w.id,w])),[watchers])
  const [testBusy,setTestBusy]=useState(false)
  const [testStatus,setTestStatus]=useState("")
  const testWebhook=async()=>{
    setTestBusy(true); setTestStatus("")
    try {
      const result=await api.testWatcherNotification()
      setTestStatus(result.ok?"测试通知已由 Hermes 投递，请检查飞书。":"Webhook 测试未完成")
    } catch(e) {
      setTestStatus(e instanceof Error?`测试失败：${e.message}`:"Webhook 测试失败")
    } finally { setTestBusy(false) }
  }
  return <div className="space-y-4">
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-4">
        <div><h2 className="font-semibold">定时任务</h2><p className="mt-1 text-xs text-slate-500">P20 Watcher · 自动检查来源；只有发现新内容时才创建正式传输任务。</p>{testStatus&&<p role="status" className="mt-2 text-xs text-cyan-300">{testStatus}</p>}</div>
        <Button disabled={testBusy} onClick={testWebhook}>{testBusy?<LoaderCircle className="animate-spin" size={14}/>:<Activity size={14}/>}测试 Webhook</Button>
      </div>
      <div className="grid gap-3 border-t border-white/6 p-4 md:grid-cols-2">
        {watchers.map(w=><div key={w.id} className="rounded-xl border border-white/8 bg-black/10 p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="font-medium text-slate-200">{w.name}</span><Badge className={w.enabled?"border-emerald-400/20 bg-emerald-400/10 text-emerald-200":"border-white/8 bg-white/5 text-slate-500"}>{w.enabled?"启用":"暂停"}</Badge></div><div className="mt-1 font-mono text-[11px] text-slate-600">{w.id}</div></div>
            <Clock3 size={17} className="shrink-0 text-slate-500"/>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
            <div><div className="text-xs text-slate-600">频率</div><div className="mt-1 text-slate-300">{intervalLabel(w.interval_seconds)}</div></div>
            <div><div className="text-xs text-slate-600">来源</div><div className="mt-1 text-slate-300">{w.kind==="github_release"?"GitHub Release":w.kind==="sparkle_appcast"?"Sparkle Appcast":w.kind}</div></div>
            <div className="col-span-2"><div className="text-xs text-slate-600">最近结果</div><div className="mt-1 flex flex-wrap items-center gap-2"><Badge className={watcherOutcomeTone(w.last_outcome)}>{watcherOutcomeLabel[w.last_outcome||""]||w.last_outcome||"尚未执行"}</Badge>{w.last_item_title&&<span className="truncate text-slate-400">{w.last_item_title}</span>}</div></div>
            <div><div className="text-xs text-slate-600">最近检查</div><div className="mt-1 text-slate-400">{formatTime(w.last_run_at)}</div></div>
            <div><div className="text-xs text-slate-600">下次检查</div><div className="mt-1 text-slate-400">{formatTime(w.next_run_at)}</div></div>
          </div>
          {w.last_message&&<div className="mt-3 rounded-lg bg-white/[.025] px-3 py-2 text-xs text-slate-500">{w.last_message}</div>}
          {w.last_job_number_label&&<button onClick={()=>{const j=jobs.find(x=>x.id===w.last_job_id);if(j)onSelectJob(j)}} className="mt-3 text-xs text-cyan-300 hover:underline">最近传输：{w.last_job_number_label} · {w.last_job_state?stateLabel[w.last_job_state]:"—"}</button>}
        </div>)}
        {!watchers.length&&<div className="col-span-2 py-10 text-center text-sm text-slate-600">还没有定时任务</div>}
      </div>
    </Card>
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between px-4 py-4"><div><h2 className="font-semibold">最近执行</h2><p className="mt-1 text-xs text-slate-500">成功、跳过、建立基线和失败都会留下记录。</p></div><Badge>{runs.length} records</Badge></div>
      <div className="grid grid-cols-[170px_minmax(0,1.5fr)_minmax(0,2fr)_150px] gap-3 border-t border-white/6 bg-black/10 px-4 py-2 text-[11px] uppercase tracking-[.12em] text-slate-600 max-md:hidden"><span>时间</span><span>任务</span><span>结果</span><span>P20 传输</span></div>
      {runs.map(run=>{const task=byId.get(run.watcher_id);const job=jobs.find(j=>j.id===run.job_id);return <div key={run.id} className="grid grid-cols-[170px_minmax(0,1.5fr)_minmax(0,2fr)_150px] gap-3 border-t border-white/6 px-4 py-3 text-sm max-md:grid-cols-1">
        <div className="text-slate-500">{formatTime(run.finished_at||run.started_at)}</div>
        <div><div className="text-slate-300">{task?.name||run.watcher_id}</div><div className="mt-1 truncate text-xs text-slate-600">{run.item_title||"—"}</div></div>
        <div><Badge className={watcherOutcomeTone(run.outcome)}>{watcherOutcomeLabel[run.outcome]||run.outcome}</Badge>{run.message&&<div className="mt-1 text-xs text-slate-500">{run.message}</div>}</div>
        <div>{run.job_number_label?<button onClick={()=>job&&onSelectJob(job)} className="text-cyan-300 hover:underline">{run.job_number_label}</button>:<span className="text-slate-600">—</span>}{run.job_state&&<div className="mt-1 text-xs text-slate-500">{stateLabel[run.job_state]}</div>}</div>
      </div>})}
      {!runs.length&&<div className="grid min-h-36 place-items-center border-t border-white/6 text-sm text-slate-600">暂无执行记录</div>}
    </Card>
  </div>
}

export default function App() {
  const [health,setHealth]=useState<Health|null>(null)
  const [jobs,setJobs]=useState<Job[]>([])
  const [watchers,setWatchers]=useState<WatcherTask[]>([])
  const [watcherRuns,setWatcherRuns]=useState<WatcherRun[]>([])
  const [section,setSection]=useState<"jobs"|"watchers">("jobs")
  const [ready,setReady]=useState(false)
  const [selected,setSelected]=useState<Job|null>(null)
  const [selectedEvents,setSelectedEvents]=useState<JobEvent[]>([])
  const [newOpen,setNewOpen]=useState(false)
  const [error,setError]=useState("")
  const refreshBusy=useRef(false)

  const refresh=useCallback(async()=>{
    if(refreshBusy.current)return
    refreshBusy.current=true
    try {
      const [h,j,w,r]=await Promise.all([api.health(),api.jobs(),api.watchers(),api.watcherRuns(100)])
      setHealth(h); setJobs(j); setWatchers(w); setWatcherRuns(r); setError("")
    } catch(e) {
      try { setHealth(await api.health()) } catch {}
      setError(e instanceof Error?e.message:"刷新失败")
    } finally { setReady(true); refreshBusy.current=false }
  },[])

  useEffect(()=>{ void refresh() },[refresh])
  useEffect(()=>{
    if(!ready)return
    const timer=setInterval(refresh,4000)
    const es=new EventSource("/api/v1/events")
    const onEvent=()=>refresh()
    ;["job.created","job.state","job.progress","job.upload_progress","job.download_message","job.ttl_expired"].forEach(t=>es.addEventListener(t,onEvent))
    return()=>{clearInterval(timer);es.close()}
  },[ready,refresh])

  useEffect(()=>{
    if(!selected)return
    const fresh=jobs.find(j=>j.id===selected.id)
    if(!fresh)return
    setSelected(current=>current ? {...fresh, archive_password: current.archive_password ?? fresh.archive_password} : current)
  },[jobs, selected?.id])

  useEffect(()=>{
    if(!selected)return
    api.jobEvents(selected.id).then(setSelectedEvents).catch(()=>{})
  },[selected?.id, selected?.updated_at])

  const active=useMemo(()=>jobs.filter(j=>activeStates.has(j.state)),[jobs])
  const down=active.reduce((s,j)=>s+(j.download_speed||0),0)
  const up=active.reduce((s,j)=>s+(j.upload_speed||0),0)
  const diskPct=health?.disk.total ? health.disk.used/health.disk.total*100 : 0

  async function clearHistory(){
    if(!window.confirm("删除全部已完成、失败和已取消任务的历史记录及本地文件？正在处理和可取回任务会保留。"))return
    try{const result=await api.clearHistory();setSelected(null);setSelectedEvents([]);await refresh();setError(`已清理 ${result.deleted} 条终态记录`)}catch(e){setError(e instanceof Error?e.message:"清理历史失败")}
  }
  async function clearOne(id:string){
    if(!window.confirm("删除这条已完成、失败或已取消任务的历史记录及本地文件？"))return
    try{await api.clearHistoryJob(id);setSelected(null);setSelectedEvents([]);await refresh()}
    catch(e){setError(e instanceof Error?e.message:"清理历史失败");throw e}
  }
  async function action(kind:"pause"|"resume"|"cancel"|"retry"|"ack",id:string){
    const updated = await api[kind](id)
    await refresh()
    const targetId = updated.id || id
    const [latest, events] = await Promise.all([api.job(targetId,true), api.jobEvents(targetId)])
    setSelected(latest)
    setSelectedEvents(events)
  }
  async function select(job:Job){
    try {
      const [latest, events] = await Promise.all([api.job(job.id,true), api.jobEvents(job.id)])
      setSelected(latest)
      setSelectedEvents(events)
    } catch {
      setSelected(job)
      setSelectedEvents([])
    }
  }

  async function stopRuntime(){
    try { await api.runtimeStop(); await refresh() }
    catch(e){ setError(e instanceof Error?e.message:"关闭 QQ Runtime 失败") }
  }

  if(!ready)return <main className="grid min-h-screen place-items-center bg-[#08111f] text-slate-400"><LoaderCircle className="animate-spin"/></main>

  return <main className="min-h-screen bg-[#08111f] text-slate-100">
    <div className="mx-auto max-w-[1480px] px-5 py-5 md:px-8 md:py-7">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="grid size-11 place-items-center rounded-2xl bg-cyan-400/12 text-cyan-300"><Network size={22}/></div>
          <div><h1 className="text-lg font-semibold">Tokyo Transfer Hub</h1><p className="text-xs text-slate-500">P20 · Transfer Control Plane</p></div>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={()=>setNewOpen(true)} className="bg-cyan-400 text-slate-950 hover:bg-cyan-300"><Plus size={16}/>新建传输</Button>
          {health?.runtime?.state==="running"&&<Button onClick={stopRuntime} title="关闭 QQ 运行时以释放内存">关闭 QQ</Button>}
          <Button onClick={refresh} title="刷新"><RefreshCcw size={15}/></Button>
        </div>
      </header>

      {error&&<div className="mb-4 rounded-xl border border-rose-400/15 bg-rose-400/8 p-3 text-sm text-rose-200">{error}</div>}

      <section className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-7">
        <StatCard icon={<Server size={17}/>} label="Tokyo" value={health?.ok?"ONLINE":"OFFLINE"} sub={health?.version ? "v"+health.version:"—"}/>
        <StatCard icon={<Network size={17}/>} label="QQ Runtime" value={health?.runtime?.state==="running"?(health?.qq_flash.qq?.user_id?"ONLINE":"STARTING"):(health?.runtime?.state==="stopped"?"STOPPED":"—")} sub={health?.runtime?.state==="running"?(health?.qq_flash.qq?.nickname || "—"):"按需启动"}/>
        <StatCard icon={<Network size={17}/>} label="QQ Flash" value={health?.qq_flash.healthy?"READY":health?.runtime?.state==="stopped"?"SLEEP":"DOWN"} sub={health?.qq_flash.healthy ? "pending "+String(health.qq_flash.flash_pending || 0)+" · spool "+String(health.qq_flash.spool_files || 0) : health?.runtime?.state==="stopped" ? "随 QQ Runtime 按需唤醒" : (health?.qq_flash.error || "不可用")}/>
        <StatCard icon={<HardDrive size={17}/>} label="Disk" value={diskPct.toFixed(0)+"%"} sub={health?formatBytes(health.disk.free)+" 可用":"—"}/>
        <StatCard icon={<ArrowDownToLine size={17}/>} label="Download" value={formatSpeed(down)} sub={active.length+" 个活跃任务"}/>
        <StatCard icon={<ArrowUpFromLine size={17}/>} label="Upload" value={formatSpeed(up)} sub="当前总上传"/>
        <StatCard icon={<Activity size={17}/>} label="Jobs" value={String(jobs.length)} sub={jobs.filter(j=>j.state==="FAILED").length+" 个失败"}/>
      </section>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Button onClick={()=>setSection("jobs")} className={section==="jobs"?"bg-cyan-400 text-slate-950 hover:bg-cyan-300":""}><ListChecks size={15}/>传输任务</Button>
        <Button onClick={()=>setSection("watchers")} className={section==="watchers"?"bg-violet-400 text-slate-950 hover:bg-violet-300":""}><Clock3 size={15}/>定时任务 <Badge>{watchers.length}</Badge></Button>
      </div>

      {section==="watchers" ? <WatcherPanel watchers={watchers} runs={watcherRuns} jobs={jobs} onSelectJob={select}/> : <div>
      <Card className="overflow-hidden">
        <div className="flex items-center justify-between px-4 py-4">
          <div><h2 className="font-semibold">传输任务</h2><p className="mt-1 text-xs text-slate-500">下载 / VPS 本地文件 → 校验 → 打包 / 加密 → 投递 → ACK → 清理</p></div>
          <div className="flex items-center gap-3"><Badge>{active.length} active</Badge><Button onClick={clearHistory}>清理已完成历史</Button></div>
        </div>
        <div className="grid grid-cols-[minmax(0,2.2fr)_130px_1fr_110px_120px_28px] gap-4 border-t border-white/6 bg-black/10 px-4 py-2 text-[11px] uppercase tracking-[.12em] text-slate-600 max-lg:hidden">
          <span>任务</span><span>下载速度</span><span>上传速度</span><span>ETA</span><span>投递</span><span/>
        </div>
        {jobs.length
          ? jobs.map(j=><JobRow key={j.id} job={j} onSelect={select}/>)
          : <div className="grid min-h-48 place-items-center border-t border-white/6 text-sm text-slate-600"><div className="text-center"><Database className="mx-auto mb-2" size={24}/><div>还没有传输任务</div></div></div>}
      </Card>
      </div>}
    </div>
    {selected&&<Detail job={selected} events={selectedEvents} onClose={()=>{setSelected(null);setSelectedEvents([])}} onAction={action} onClear={clearOne}/>}
    {newOpen&&<NewTransfer onClose={()=>setNewOpen(false)} onCreated={(j)=>{setJobs(x=>[j,...x]);setSelected(j)}}/>}
  </main>
}
