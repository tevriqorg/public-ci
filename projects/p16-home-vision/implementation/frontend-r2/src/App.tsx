import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { playbackResult, selectEnqueueCandidates, selectRecordingJob } from './archiveBehavior.js'

const pages = [
  { id: 'overview', label: '总览', icon: '◷' },
  { id: 'archive', label: '归档', icon: '▤' },
  { id: 'analysis', label: '分析', icon: '⌁' },
  { id: 'tasks', label: '任务', icon: '≡' },
  { id: 'review', label: '待复核', icon: '◇' },
  { id: 'history', label: '记录', icon: '↺' },
] as const

type PageId = (typeof pages)[number]['id']
type Asset = {
  asset_id: string
  source_recording_id?: string
  camera?: string
  started_at?: string | null
  state?: string
  raw_deleted?: boolean
  verification_state?: string
  lifecycle_state?: string
  size_bytes?: number
  media_duration_ms?: number
  video_codec?: string
  width?: number | null
  height?: number | null
  normalization_state?: string | null
  normalization_progress_percent?: number | null
  normalization_error_code?: string | null
  stages?: Record<string, string>
  result?: { frame_count?: number }
  error_code?: string
}
type Recording = { source_recording_id: string; track_id?: string; source_start?: string; source_end?: string; duration_ms?: number; source_size_if_known?: number | null; source_state?: string; archive_status?: string; thumbnail_available?: boolean }
type Job = { archive_job_id: string; source_recording_id: string; state: string; requested_at?: string; bytes_received?: number; source_size_bytes?: number; progress_percent?: number; throughput_bytes_per_second?: number; error_code?: string }
type Camera = { track_id: string; name?: string }
type Review = { review_flag_id?: string; asset_id: string; flag_type?: string; review_state?: string; confidence?: number | null }
type Audit = { audit_event_id?: string; created_at?: string; event_type?: string; entity_type?: string; entity_id?: string; payload?: Record<string, unknown> }
type AnalysisRun = { analysis_run_id: string; asset_id: string; state: string; analysis_profile_id?: string; analysis_profile_revision?: number; pipeline_version?: string; model_id?: string; completed_at?: string }
type Event = { analysis_run_id: string; start_offset_ms?: number; summary: string }
type Volume = { storage_volume_id: string; online?: boolean; writable?: boolean; total_bytes?: number; free_bytes?: number; application_used_bytes?: number }
type Snapshot = {
  assets: Asset[]; recordings: Recording[]; scans: Array<{started_at?: string; state?: string; scanned_at?: string}>; jobs: Job[]; cameras: Camera[]; reviews: Review[]; audits: Audit[]
  runs: AnalysisRun[]; events: Event[]; volumes: Volume[]; archive?: { enqueue_ready?: boolean; configured?: boolean; control_actions?: string[]; heartbeat?: { status?: string } }
  runtime?: { runtime?: string; runtime_version?: string; cpu?: { load_1m?: number | null; logical_cores?: number | null }; memory?: { used_bytes?: number | null; limit_bytes?: number | null }; host?: { status?: string; reason?: string } }
  error?: string
}

const labelForState: Record<string, string> = {
  complete: '完整', incomplete: '未完成', scanned_no_recording: '已扫描·无录像', partial: '部分完成', failed: '失败', unknown: '未扫描', today: '今天',
  available: '可用', expired: '源已失效', verified: '已验证', archived_verified: '已归档', queued: '等待中', paused: '已暂停', resolving_source: '定位源录像', downloading: '拉取中', verifying: '校验中', completed: '已完成', cancelled: '已取消',
  pending: '等待中', running: '进行中', processing: '处理中', idle: '未开始', done: '已完成', skipped: '未执行',
}
const stageLabels: Record<string, string> = { raw: '原始录像', web: 'Web Media', frames: '抽帧', audio: '音频', transcript: '转录', reduce: '关键帧筛选', package: '分析包' }
const jobStateLabel: Record<string, string> = { queued: '等待中', resolving_source: '定位源录像', downloading: '拉取中', verifying: '校验中', paused: '已暂停', completed: '已完成', failed: '失败', cancelled: '已取消' }
const actionLabels: Record<string, string> = { pause_queued: '暂停排队任务', cancel_waiting: '取消等待任务', resume_paused: '继续暂停任务', retry_failed: '重试失败任务' }
const actionEndpoints: Record<string, string> = { pause_queued: '/internal/archive/jobs/pause-all', cancel_waiting: '/internal/archive/jobs/cancel-unfinished', resume_paused: '/internal/archive/jobs/resume-all', retry_failed: '/internal/archive/jobs/retry-failed' }
const formatDate = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)) : '时间未知'
const formatBytes = (value?: number) => {
  if (value == null || !Number.isFinite(value)) return '未知'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let size = value; let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`
}
const formatDuration = (value?: number | null) => value == null ? '时长未知' : `${Math.floor(value / 60)}分${Math.round(value % 60)}秒`
const dateKey = (value?: string) => value?.slice(0, 10) || 'unknown'

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' }, cache: 'no-store' })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json() as Promise<T>
}
async function getItems<T>(path: string, pageSize = 500): Promise<T[]> {
  const items: T[] = []; let after: string | null = null
  do {
    const url = new URL(path, window.location.origin); url.searchParams.set('limit', String(pageSize)); if (after) url.searchParams.set('after_id', after)
    const page = await getJson<{ items?: T[]; next_after_id?: string }>(url.pathname + url.search)
    const rows = page.items ?? []; items.push(...rows)
    after = page.next_after_id || null
  } while (after)
  return items
}
async function fetchSnapshot(): Promise<Snapshot> {
  const [assets, recordings, scans, jobs, cameras, volumes, archive, runtime] = await Promise.all([
    getItems<Asset>('/api/v1/assets'), getItems<Recording>('/api/v1/recordings'), getItems<Snapshot['scans'][number]>('/api/v1/scan-runs'), getItems<Job>('/api/v1/archive-jobs'),
    getItems<Camera>('/api/v1/cameras'), getItems<Volume>('/api/v1/storage-volumes'), getJson<Snapshot['archive']>('/internal/archive/status'), getJson<Snapshot['runtime']>('/api/v1/runtime-status'),
  ])
  return { assets, recordings, scans, jobs, cameras, volumes, archive, runtime, reviews: [], audits: [], runs: [], events: [] }
}
async function fetchSecondary(page: PageId): Promise<Pick<Snapshot, 'reviews' | 'audits' | 'runs' | 'events'>> {
  if (page === 'review') return { reviews: await getItems<Review>('/api/v1/review-flags'), audits: [], runs: [], events: [] }
  if (page === 'history') return { reviews: [], audits: await getItems<Audit>('/api/v1/audit-events'), runs: [], events: [] }
  if (page === 'analysis') {
    const [runs, events] = await Promise.all([getItems<AnalysisRun>('/api/v1/analysis-runs'), getItems<Event>('/api/v1/events')])
    return { reviews: [], audits: [], runs, events }
  }
  return { reviews: [], audits: [], runs: [], events: [] }
}
async function postAction(action: string): Promise<void> {
  const endpoint = actionEndpoints[action]
  if (!endpoint) throw new Error('不支持的归档控制动作')
  const response = await fetch(endpoint, { method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: '{}' })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
}
async function enqueueRecordings(ids: string[]): Promise<{ queued_count?: number }> {
  const response = await fetch('/internal/archive/batch', { method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify({ source_recording_ids: ids }) })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}
async function resolvePlayback(assetId: string): Promise<{ playback_path?: string; status?: string }> {
  return getJson(`/api/v1/media/${encodeURIComponent(assetId)}/access`)
}

function Status({ value }: { value?: string }) {
  const state = value ?? 'unknown'; const tone = ['completed', 'complete', 'done', 'verified', 'archived_verified'].includes(state) ? 'good' : ['failed', 'expired'].includes(state) ? 'bad' : ['running', 'downloading', 'processing'].includes(state) ? 'active' : 'muted'
  return <span className={`badge ${tone}`}><i />{labelForState[state] ?? state}</span>
}
function Empty({ title, children }: { title: string; children: string }) { return <div className="empty"><span className="empty-mark">—</span><strong>{title}</strong><p>{children}</p></div> }
function Metric({ label, value, note }: { label: string; value: string; note?: string }) { return <article className="metric"><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</article> }
function AssetStages({ asset }: { asset: Asset }) {
  const stages = asset.stages ?? {}
  return <div className="stage-list">{Object.entries(stages).map(([name, state]) => <div className="stage-line" key={name}><span>{stageLabels[name] ?? name}</span><Status value={state} /></div>)}{!Object.keys(stages).length && <p className="muted-text">该资源尚无阶段明细。</p>}{asset.error_code && <p className="error-text">错误代码：{asset.error_code}</p>}</div>
}
function FloatingPlayer({ item, onClose, onStep }: { item: Asset | null; onClose: () => void; onStep: (step: number) => void }) {
  const [large, setLarge] = useState(false); const [minimized, setMinimized] = useState(false)
  const [playback, setPlayback] = useState<{ path?: string; message: string }>({ message: '正在确认可播放资源…' })
  useEffect(() => {
    let current = true
    setPlayback({ message: '正在确认可播放资源…' })
    if (item) void resolvePlayback(item.asset_id).then((access) => {
      if (!current) return
      setPlayback(playbackResult(access))
    }).catch(() => { if (current) setPlayback({ message: '播放信息暂不可用，请稍后重试。' }) })
    return () => { current = false }
  }, [item?.asset_id])
  if (!item) return null
  return <aside className={`floating-player ${large ? 'large' : ''} ${minimized ? 'minimized' : ''}`} aria-label="浮动播放器">
    <header><strong title={item.asset_id}>{item.camera || item.asset_id} · {formatDate(item.started_at)}</strong><div><button aria-label="最小化播放器" onClick={() => setMinimized(!minimized)}>−</button><button aria-label="放大播放器" onClick={() => setLarge(!large)}>□</button><button aria-label="关闭播放器" onClick={onClose}>×</button></div></header>
    {!minimized && <>{playback.path ? <video controls autoPlay playsInline src={playback.path} /> : <div className="player-message" role="status">{playback.message}</div>}<footer><button onClick={() => onStep(-1)}>上一段</button><span>页面与滚动上下文保持不变</span><button onClick={() => onStep(1)}>下一段</button></footer></>}
  </aside>
}
function App() {
  const [page, setPage] = useState<PageId>('overview'); const [month, setMonth] = useState(() => new Date(new Date().getFullYear(), new Date().getMonth(), 1)); const [selectedDay, setSelectedDay] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null); const [player, setPlayer] = useState<Asset | null>(null); const [toast, setToast] = useState(''); const [archiveFilter, setArchiveFilter] = useState('all'); const [actionPending, setActionPending] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<string[]>([]); const [enqueuePending, setEnqueuePending] = useState(false); const [enqueuePreview, setEnqueuePreview] = useState<string[] | null>(null)
  const query = useQuery({ queryKey: ['product', 'catalog'], queryFn: fetchSnapshot, refetchInterval: 5000, refetchOnWindowFocus: true, placeholderData: (previous) => previous })
  const secondary = useQuery({ queryKey: ['product', 'page', page], queryFn: () => fetchSecondary(page), enabled: ['analysis', 'review', 'history'].includes(page), refetchInterval: 15000 })
  const snapshot = { ...(query.data ?? { assets: [], recordings: [], scans: [], jobs: [], cameras: [], volumes: [], reviews: [], audits: [], runs: [], events: [] }), ...(secondary.data ?? {}) } as Snapshot
  const assets = snapshot.assets ?? []; const recordings = snapshot.recordings ?? []; const jobs = snapshot.jobs ?? []
  const assetFor = (recording: Recording) => assets.find((asset) => asset.source_recording_id === recording.source_recording_id)
  const jobFor = (recording: Recording) => selectRecordingJob(jobs, recording.source_recording_id)
  const recordingsByDay = useMemo(() => {
    const grouped = new Map<string, Recording[]>(); [...recordings].sort((a, b) => String(b.source_start).localeCompare(String(a.source_start))).forEach((recording) => { const key = dateKey(recording.source_start); grouped.set(key, [...(grouped.get(key) ?? []), recording]) }); return grouped
  }, [recordings])
  const calendarCells = useMemo(() => {
    const year = month.getFullYear(); const monthIndex = month.getMonth(); const count = new Date(year, monthIndex + 1, 0).getDate(); const start = new Date(year, monthIndex, 1).getDay()
    return [...Array(start).fill(null), ...Array.from({ length: count }, (_, index) => index + 1)]
  }, [month])
  const dayStatus = (day: number) => {
    const key = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`; const list = recordingsByDay.get(key) ?? []
    if (!list.length) {
      if (dateKey(new Date().toISOString()) === key) return 'today'
      const scan = snapshot.scans.find((run) => dateKey(run.started_at ?? run.scanned_at) === key)
      if (scan?.state === 'completed') return 'scanned_no_recording'
      if (scan?.state === 'partial') return 'partial'
      if (scan?.state === 'failed') return 'failed'
      return 'unknown'
    }
    if (list.every((row) => row.archive_status === 'archived_verified')) return 'complete'
    if (list.some((row) => jobFor(row)?.state === 'failed')) return 'failed'
    if (list.some((row) => ['available', 'expired'].includes(row.source_state ?? ''))) return 'incomplete'
    return 'partial'
  }
  const cameraName = (id?: string) => snapshot.cameras.find((camera) => String(camera.track_id) === String(id))?.name || `摄像头 ${id ?? '未知'}`
  const activeJobs = jobs.filter((job) => ['queued', 'paused', 'resolving_source', 'downloading', 'verifying'].includes(job.state))
  const newest = [...recordings].sort((a, b) => String(b.source_start).localeCompare(String(a.source_start)))
  const reviewedRuns = snapshot.runs ?? []; const events = snapshot.events ?? []; const reviews = snapshot.reviews ?? []; const audits = snapshot.audits ?? []
  const playables = assets.filter((asset) => asset.verification_state === 'verified' && asset.lifecycle_state === 'present')
  const flash = (message: string) => { setToast(message); window.setTimeout(() => setToast(''), 2800) }
  const performAction = async (action: string) => {
    if (!(snapshot.archive?.control_actions ?? []).includes(action)) return flash('服务端当前未声明此控制能力。')
    if (action === 'cancel_waiting') {
      const count = jobs.filter((job) => ['queued', 'paused'].includes(job.state)).length
      if (!count || !window.confirm(`将取消 ${count} 个等待中的归档任务；进行中的任务不会被中断。确认继续吗？`)) return
    }
    setActionPending(action)
    try { await postAction(action); flash(`${actionLabels[action] ?? action}请求已提交`); await query.refetch() }
    catch (error) { flash(`控制请求失败：${String(error)}`) }
    finally { setActionPending(null) }
  }
  const navigate = (next: PageId) => { setPage(next); window.history.replaceState(null, '', `#${next}`); window.scrollTo({ top: 0, behavior: 'auto' }) }
  const openPlayer = (asset: Asset) => setPlayer(asset)
  const stepPlayer = (step: number) => { const index = playables.findIndex((asset) => asset.asset_id === player?.asset_id); const next = playables[index + step]; if (next) setPlayer(next) }
  const canEnqueue = (recording: Recording) => snapshot.archive?.enqueue_ready === true
    && recording.archive_status !== 'archived_verified'
    && !['queued', 'resolving_source', 'downloading', 'verifying', 'paused'].includes(jobFor(recording)?.state ?? '')
  const previewEnqueue = (ids: string[]) => {
    const eligible = selectEnqueueCandidates(ids, recordings, jobFor, snapshot.archive?.enqueue_ready === true)
    if (!eligible.length) return flash('没有符合当前服务端 readiness 与状态条件的录像。')
    setEnqueuePreview(eligible)
  }
  const confirmEnqueue = async () => {
    if (!enqueuePreview?.length || !snapshot.archive?.enqueue_ready) return
    setEnqueuePending(true)
    try {
      const result = await enqueueRecordings(enqueuePreview)
      setSelectedIds([]); setEnqueuePreview(null)
      flash(`归档请求已提交：${result.queued_count ?? enqueuePreview.length} 段`)
      await query.refetch()
    } catch (error) { flash(`归档请求失败：${String(error)}`) }
    finally { setEnqueuePending(false) }
  }

  return <div className="app-frame">
    <aside className="rail"><div className="brand"><span className="brand-icon">P</span><span>P16<small>HOME VISION</small></span></div><div className="rail-caption">家庭媒体</div><nav aria-label="主导航">{pages.map((item) => <button key={item.id} className={page === item.id ? 'selected' : ''} aria-current={page === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}><span className="nav-icon">{item.icon}</span><span>{item.label}</span></button>)}</nav><div className="rail-foot"><span className={`connection ${query.isError ? 'offline' : ''}`} /><small>{query.isError ? 'API 不可用' : '本地服务'}</small></div></aside>
    <main className="main-area"><header className="topbar"><div><span className="crumb">P16 / {pages.find((item) => item.id === page)?.label}</span><small>{query.isError ? '连接失败 · 显示最近成功读取数据' : query.isFetching ? '正在同步状态' : '数据状态来自当前 API'}</small></div><div className="topbar-right"><span className="sync-dot" /><span>状态自动更新</span><button className="icon-button" aria-label="刷新数据" onClick={() => { void query.refetch(); void secondary.refetch() }}>↻</button></div></header>
      <div className="content-area">{query.isError && <div className="alert" role="status"><strong>服务暂不可用</strong><span>{String(query.error)}。页面保留最近一次已读取状态，不生成模拟数据。</span><button onClick={() => void query.refetch()}>重试</button></div>}
        {page === 'overview' && <section className="page-view"><div className="page-heading"><div><span className="eyebrow">家庭录像 · 总体状态</span><h1>总览</h1><p>先按日期查看录像覆盖与归档欠账，再检查运行状态。</p></div><Status value={query.isError ? 'failed' : query.isFetching ? 'running' : 'completed'} /></div>
          <div className="overview-layout"><section className="calendar-panel"><div className="calendar-header"><div><span className="eyebrow">录像覆盖</span><h2>{new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long' }).format(month)}</h2></div><div><button className="icon-button light" aria-label="上个月" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}>‹</button><button className="icon-button light" aria-label="下个月" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}>›</button></div></div><div className="calendar-grid">{['日', '一', '二', '三', '四', '五', '六'].map((day) => <span className="weekday" key={day}>{day}</span>)}{calendarCells.map((day, index) => day ? <button key={index} className={`calendar-day ${dayStatus(day)} ${selectedDay === `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}` ? 'picked' : ''}`} onClick={() => { const key = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`; setSelectedDay(key); if (recordingsByDay.has(key)) navigate('archive') }} aria-label={`${month.getFullYear()}年${month.getMonth() + 1}月${day}日：${labelForState[dayStatus(day)]}`}><b>{day}{dayStatus(day) === 'today' ? ' ·' : ''}</b><small>{(recordingsByDay.get(`${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`) ?? []).length || '—'}</small></button> : <span key={index} />)}</div><div className="calendar-legend">{[['complete','已归档'], ['incomplete','待归档'], ['partial','部分'], ['failed','失败'], ['unknown','未知'], ['today','今天']].map(([tone, text]) => <span key={tone}><i className={tone} />{text}</span>)}</div><div className="calendar-foot">日期状态只依据已返回的录像清单；空白日期未必已完成扫描。</div></section>
            <div className="overview-side"><section className="summary-panel"><div className="section-title"><div><span className="eyebrow">归档概况</span><h2>真实资产状态</h2></div></div><div className="metric-grid"><Metric label="录像段" value={String(recordings.length)} note="当前 API 清单"/><Metric label="已验证" value={String(assets.filter((asset) => asset.verification_state === 'verified').length)} note="本地已验证副本"/><Metric label="未归档" value={String(recordings.filter((item) => item.archive_status !== 'archived_verified').length)} note="来源清单中的欠账"/><Metric label="进行中任务" value={String(activeJobs.length)} note="排队、拉取或校验"/></div></section>
              <section className="summary-panel storage-panel"><div className="section-title"><div><span className="eyebrow">独立存储状态</span><h2>归档卷 A / B</h2></div></div>{snapshot.volumes.length ? <div className="volume-list">{snapshot.volumes.slice(0, 2).map((volume) => <div className="volume-row" key={volume.storage_volume_id}><div><strong>{volume.storage_volume_id}</strong><span>{volume.online ? (volume.writable ? '在线 · 可写' : '在线 · 只读') : '离线'}</span></div><div className="volume-value">{formatBytes(volume.free_bytes)} 可用</div>{volume.total_bytes ? <div className="meter"><span style={{ width: `${Math.min(100, Math.max(0, 100 * ((volume.total_bytes - (volume.free_bytes ?? 0)) / volume.total_bytes))) }%` }} /></div> : <small>容量数据不可用</small>}</div>)}</div> : <p className="muted-text">当前 API 未返回存储卷。</p>}</section>
              <section className="summary-panel telemetry-panel"><div className="section-title"><div><span className="eyebrow">运行遥测</span><h2>Home Vision</h2></div></div>{snapshot.runtime ? <div className="telemetry-grid"><span>运行环境 <b>{snapshot.runtime.runtime ?? '未知'}</b></span><span>CPU 核心 <b>{snapshot.runtime.cpu?.logical_cores ?? '未知'}</b></span><span>负载 <b>{snapshot.runtime.cpu?.load_1m ?? '未知'}</b></span><span>内存 <b>{formatBytes(snapshot.runtime.memory?.used_bytes ?? undefined)} / {formatBytes(snapshot.runtime.memory?.limit_bytes ?? undefined)}</b></span></div> : <p className="muted-text">容器遥测不可用。</p>}<div className="unavailable-note">主机级遥测尚未接入；不代表主机在线状态。</div></section>
              <section className="summary-panel recent-panel"><div className="section-title"><div><span className="eyebrow">最近录像</span><h2>近期活动</h2></div><button className="text-button" onClick={() => navigate('archive')}>打开归档 →</button></div>{newest.slice(0, 4).map((item) => <div className="recent-row" key={item.source_recording_id}><span>{formatDate(item.source_start)}</span><strong>{cameraName(item.track_id)}</strong><Status value={item.archive_status === 'archived_verified' ? 'verified' : item.source_state}/></div>)}{!newest.length && <p className="muted-text">暂无可展示的录像活动。</p>}</section></div></div></section>}
        {page === 'archive' && <section className="page-view"><div className="page-heading"><div><span className="eyebrow">日期 → 摄像头 → 录像段</span><h1>归档</h1><p>按日期、摄像头 lane 和独立录像资源浏览；任务能力以服务端 readiness 为准。</p></div><button className="button-secondary" onClick={() => void query.refetch()}>刷新清单</button></div><div className="archive-toolbar"><label>筛选状态 <select value={archiveFilter} onChange={(event) => setArchiveFilter(event.target.value)}><option value="all">全部</option><option value="unarchived">未归档</option><option value="failed">失败</option><option value="expired">源已失效</option></select></label><span>{recordings.length} 段真实录像 · 已选 {selectedIds.length}</span><button className="button-secondary" disabled={!selectedIds.length || !snapshot.archive?.enqueue_ready} onClick={() => previewEnqueue(selectedIds)}>预览已选</button><button className="button-primary" disabled={!snapshot.archive?.enqueue_ready || !selectedIds.length} title={!snapshot.archive?.enqueue_ready ? '归档 worker 尚未就绪' : '选择可归档录像后先预览'} onClick={() => previewEnqueue(selectedIds)}>批量预览</button></div>
          {recordings.length ? [...recordingsByDay.entries()].filter(([day]) => selectedDay ? day === selectedDay : true).map(([day, dayRows]) => {
            const visible = dayRows.filter((item) => archiveFilter === 'all' || (archiveFilter === 'unarchived' && item.archive_status !== 'archived_verified') || (archiveFilter === 'failed' && jobFor(item)?.state === 'failed') || (archiveFilter === 'expired' && item.source_state === 'expired'))
            if (!visible.length) return null
            const lanes = new Map<string, Recording[]>(); visible.forEach((recording) => { const id = String(recording.track_id ?? 'unknown'); lanes.set(id, [...(lanes.get(id) ?? []), recording]) })
            return <section className="archive-day" key={day}><header className="day-heading"><div><span className="eyebrow">日期</span><h2>{day === 'unknown' ? '日期未知' : day}</h2><small>{visible.length} 段 · newest → oldest</small></div><button className="button-secondary" disabled={!snapshot.archive?.enqueue_ready} onClick={() => previewEnqueue(visible.map((row) => row.source_recording_id))}>预览当天可归档录像</button></header>{[...lanes.entries()].map(([camera, rows]) => <div className="camera-lane" key={camera}><header><strong>{cameraName(camera)}</strong><span>{rows.length} 段</span></header><div className="segment-grid">{rows.map((recording) => { const asset = assetFor(recording); const job = jobFor(recording); const id = recording.source_recording_id; const selectable = canEnqueue(recording); return <article className="segment-card" key={id}><div className="segment-top"><label className="recording-select"><input type="checkbox" checked={selectedIds.includes(id)} disabled={!selectable} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...current, id] : current.filter((value) => value !== id))}/><span>选择</span></label><Status value={job?.state ?? (asset?.verification_state === 'verified' ? 'verified' : recording.source_state)}/></div><h3>{id}</h3><p>{formatDate(recording.source_end)} · {recording.archive_status === 'archived_verified' ? '已归档验证' : '来源录像'}</p><div className="segment-actions"><button className="button-secondary" disabled={!asset || asset.verification_state !== 'verified' || asset.lifecycle_state !== 'present'} onClick={() => asset && openPlayer(asset)}>播放</button><button className="button-primary" disabled={!selectable} onClick={() => previewEnqueue([id])}>预览归档</button><button className="icon-button" aria-label={`${expanded === id ? '收起' : '展开'}资源状态 ${id}`} onClick={() => setExpanded(expanded === id ? null : id)}>{expanded === id ? '−' : '+'}</button></div>{expanded === id && (asset ? <AssetStages asset={asset}/> : <p className="muted-text">尚无本地资产；来源与归档任务状态独立。</p>)}</article> })}</div></div>)}</section>
          }) : <Empty title="暂无录像清单" >当前 API 未返回 NVR 录像记录；页面不会用样例填充归档。</Empty>}</section>}
        {page === 'analysis' && <section className="page-view"><div className="page-heading"><div><span className="eyebrow">独立于媒体归档</span><h1>分析</h1><p>仅展示 API 返回的运行结果与来源信息，不生成模拟结论。</p></div></div>{secondary.isLoading ? <div className="loading">正在读取分析记录…</div> : secondary.isError ? <Empty title="分析 API 暂不可用">{String(secondary.error)}</Empty> : <><div className="metric-grid stats-grid"><Metric label="已完成" value={String(reviewedRuns.filter((run) => run.state === 'completed').length)}/><Metric label="等待中" value={String(reviewedRuns.filter((run) => run.state === 'pending').length)}/><Metric label="进行中" value={String(reviewedRuns.filter((run) => ['running','processing'].includes(run.state)).length)}/><Metric label="失败" value={String(reviewedRuns.filter((run) => run.state === 'failed').length)}/></div>{reviewedRuns.length ? reviewedRuns.map((run) => <article className="data-card analysis-card" key={run.analysis_run_id}><div className="card-title"><div><span className="eyebrow">分析运行 · {run.analysis_run_id}</span><h2>{run.asset_id}</h2></div><Status value={run.state}/></div><p>{run.analysis_profile_id ? `${run.analysis_profile_id} @ r${run.analysis_profile_revision ?? '未知'}` : '策略未记录'} · {run.model_id ?? '模型未记录'} · {run.pipeline_version ?? '处理版本未记录'}</p>{events.filter((event) => event.analysis_run_id === run.analysis_run_id).map((event, index) => <div className="event-row" key={index}><span>{formatDuration((event.start_offset_ms ?? 0) / 1000)}</span><p>{event.summary}</p><button className="button-secondary" disabled={!assets.some((asset) => asset.asset_id === run.asset_id && asset.verification_state === 'verified')} onClick={() => { const asset = assets.find((candidate) => candidate.asset_id === run.asset_id); if (asset) openPlayer(asset) }}>定位播放</button></div>)}<details className="provenance"><summary>查看运行来源</summary><pre>{JSON.stringify(run, null, 2)}</pre></details></article>) : <Empty title="暂无分析结果">当前 API 未返回分析运行或事件；分析数据与归档状态相互独立。</Empty>}</>}</section>}
        {page === 'tasks' && <section className="page-view"><div className="page-heading"><div><span className="eyebrow">Operational list</span><h1>任务</h1><p>归档队列操作状态、字节数和服务端明确提供的控制能力。</p></div><button className="button-secondary" onClick={() => void query.refetch()}>刷新任务</button></div><div className="metric-grid stats-grid"><Metric label="拉取中" value={String(jobs.filter((job) => job.state === 'downloading').length)}/><Metric label="等待 / 定位" value={String(jobs.filter((job) => ['queued','resolving_source'].includes(job.state)).length)}/><Metric label="校验中" value={String(jobs.filter((job) => job.state === 'verifying').length)}/><Metric label="失败" value={String(jobs.filter((job) => job.state === 'failed').length)}/><Metric label="已完成" value={String(jobs.filter((job) => job.state === 'completed').length)}/></div><div className="task-toolbar">{(snapshot.archive?.control_actions ?? []).map((action) => <button key={action} className={action === 'cancel_waiting' ? 'button-danger' : 'button-secondary'} disabled={!!actionPending || !(snapshot.archive?.control_actions ?? []).includes(action)} onClick={() => void performAction(action)}>{actionPending === action ? '提交中…' : actionLabels[action] ?? action}</button>)}{!snapshot.archive?.configured && <span className="muted-text">归档队列未配置，控制不可用。</span>}{snapshot.archive?.configured && !snapshot.archive?.enqueue_ready && <span className="muted-text">worker 未就绪；仅显示服务端报告的可用控制。</span>}</div><div className="task-list">{jobs.length ? jobs.map((job) => { const recording = recordings.find((item) => item.source_recording_id === job.source_recording_id); const pct = job.progress_percent; return <article className="task-row" key={job.archive_job_id}><div className="task-main"><strong>{cameraName(recording?.track_id)} · {formatDate(recording?.source_start)}</strong><small>任务 {job.archive_job_id} · {job.source_recording_id}</small></div><Status value={job.state}/><div className="task-progress">{typeof pct === 'number' ? <><div className="meter"><span style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}/></div><small>{pct}% · {formatBytes(job.bytes_received)} / {job.source_size_bytes ? formatBytes(job.source_size_bytes) : '总量未知'}</small></> : <><div className="meter indeterminate"><span/></div><small>{formatBytes(job.bytes_received)} · 进度百分比未知</small></>}</div>{job.error_code && <small className="error-text">{job.error_code}</small>}</article> }) : <Empty title="暂无归档任务">任务来自真实归档队列；空状态不复用录像卡片。</Empty>}</div></section>}
        {page === 'review' && <section className="page-view"><div className="page-heading"><div><span className="eyebrow">Human-governed</span><h1>待复核</h1><p>只显示真实 review flag；任何删除或身份操作都不会由此界面伪造提交。</p></div></div>{secondary.isLoading ? <div className="loading">正在读取待复核项目…</div> : secondary.isError ? <Empty title="复核 API 暂不可用">{String(secondary.error)}</Empty> : reviews.length ? <div className="review-list">{reviews.map((item, index) => <article className="data-card" key={item.review_flag_id ?? `${item.asset_id}-${index}`}><div className="card-title"><div><span className="eyebrow">{item.flag_type ?? '复核项目'}</span><h2>{item.asset_id}</h2></div><Status value={item.review_state}/></div><p>置信度：{item.confidence == null ? '未提供' : item.confidence}</p><div className="review-actions"><button className="button-secondary" disabled title="审核 mutation API 未提供">保留 / 确认</button><button className="button-danger" disabled title="RAW 物理删除仅允许明确 Human authority；此 API 未提供">删除 RAW（不可用）</button></div></article>)}</div> : <Empty title="暂无待复核项目">API 当前没有待处理的 review flag；不存在删除候选或待指认项目。</Empty>}</section>}
        {page === 'history' && <section className="page-view"><div className="page-heading"><div><span className="eyebrow">不可变审计事实</span><h1>记录</h1><p>归档生命周期、任务和控制事件仅按真实 audit 数据呈现。</p></div></div>{secondary.isLoading ? <div className="loading">正在读取审计记录…</div> : secondary.isError ? <Empty title="审计 API 暂不可用">{String(secondary.error)}</Empty> : audits.length ? <div className="audit-list">{audits.map((item, index) => <article className="audit-row" key={item.audit_event_id ?? index}><span className="audit-marker"/><div><strong>{item.event_type ?? '审计事件'}</strong><p>{item.entity_type ?? '实体'} · {item.entity_id ?? 'ID 未记录'} · {JSON.stringify(item.payload ?? {})}</p></div><time>{formatDate(item.created_at)}</time></article>)}</div> : <Empty title="暂无审计记录">当前 audit API 没有返回记录；不会填入演示历史。</Empty>}</section>}
        {secondary.isError && ['analysis','review','history'].includes(page) && <div className="alert subtle" role="status">此页面的真实数据请求失败；不会以空结果掩盖 API 错误。<button onClick={() => void secondary.refetch()}>重试</button></div>}
      </div>
    </main><FloatingPlayer item={player} onClose={() => setPlayer(null)} onStep={stepPlayer}/>{enqueuePreview && <div className="dialog-backdrop" role="presentation"><section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="enqueue-title"><h2 id="enqueue-title">确认归档队列</h2><p>将提交 {enqueuePreview.length} 段录像。再次确认前不会创建任务；服务端仍会校验当前 readiness 和重复任务状态。</p><ul>{enqueuePreview.map((id) => <li key={id}>{id}</li>)}</ul><div><button className="button-secondary" disabled={enqueuePending} onClick={() => setEnqueuePreview(null)}>返回修改</button><button className="button-primary" disabled={enqueuePending || !snapshot.archive?.enqueue_ready} onClick={() => void confirmEnqueue()}>{enqueuePending ? '提交中…' : '确认并加入队列'}</button></div></section></div>}{toast && <div className="toast" role="status">{toast}</div>}
  </div>
}
export default App
