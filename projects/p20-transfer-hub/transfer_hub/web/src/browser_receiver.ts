import type { ReceiverTicket } from "./types"

export type ReceiverPhase =
  | "RESOLVING"
  | "DOWNLOADING"
  | "DECRYPTING_EXTRACTING"
  | "SAVING"
  | "WAITING_ACK"
  | "DONE"

export type ReceiverProgressEvent = {
  phase: ReceiverPhase
  message: string
  artifact_name?: string
  percent?: number
  bytes_done?: number
  bytes_total?: number
  download_percent?: number
  download_bytes_done?: number
  download_bytes_total?: number
  speed_bps?: number
  eta_seconds?: number
  entry_index?: number
  entry_count?: number
  entry_bytes_done?: number
  entry_bytes_total?: number
  network_bytes?: number
}

export type ReceiverProgress = (progress: ReceiverProgressEvent) => void

export type ReceiverDebugEntry = {
  at: string
  phase: string
  message: string
  data?: Record<string, string | number | boolean | null>
}

export type ReceiverDebug = (entry: ReceiverDebugEntry) => void

function debug(onDebug: ReceiverDebug | undefined, phase: string, message: string, data?: ReceiverDebugEntry["data"]) {
  onDebug?.({ at: new Date().toISOString(), phase, message, data })
}

function clampPercent(value: number) {
  return Math.max(0, Math.min(100, value))
}

function progressPercent(done: number, total: number) {
  return total > 0 ? clampPercent((done / total) * 100) : undefined
}

class RateTracker {
  private lastAt = performance.now()
  private lastBytes = 0
  private smoothedBps = 0

  sample(bytes: number) {
    const now = performance.now()
    const elapsed = (now - this.lastAt) / 1000
    const delta = bytes - this.lastBytes
    if (delta > 0 && elapsed >= 0.12) {
      const instant = delta / elapsed
      this.smoothedBps = this.smoothedBps ? this.smoothedBps * 0.72 + instant * 0.28 : instant
      this.lastBytes = bytes
      this.lastAt = now
    }
    return this.smoothedBps || undefined
  }
}

class ByteCoverage {
  private ranges: Array<[number, number]> = []
  covered = 0

  add(start: number, end: number) {
    if (end <= start) return 0
    const before = this.covered
    const merged: Array<[number, number]> = []
    let nextStart = start
    let nextEnd = end
    let inserted = false
    for (const [rangeStart, rangeEnd] of this.ranges) {
      if (rangeEnd < nextStart) {
        merged.push([rangeStart, rangeEnd])
      } else if (nextEnd < rangeStart) {
        if (!inserted) {
          merged.push([nextStart, nextEnd])
          inserted = true
        }
        merged.push([rangeStart, rangeEnd])
      } else {
        nextStart = Math.min(nextStart, rangeStart)
        nextEnd = Math.max(nextEnd, rangeEnd)
      }
    }
    if (!inserted) merged.push([nextStart, nextEnd])
    this.ranges = merged
    this.covered = merged.reduce((sum, [rangeStart, rangeEnd]) => sum + rangeEnd - rangeStart, 0)
    return this.covered - before
  }
}

type ResolvedArtifact = ReceiverTicket["artifacts"][number] & { url: string }

function triggerDownload(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = name
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

export function isMobileReceiverBrowser() {
  const userAgent = navigator.userAgent || ""
  const platform = navigator.platform || ""
  return /Android|iPhone|iPad|iPod|Mobile/i.test(userAgent)
    || (platform === "MacIntel" && navigator.maxTouchPoints > 1)
}

const QQ_RESOLVE_RETRY_DELAYS_MS = [1000, 2000, 3000, 4000]

async function resolveOneArtifact(
  artifact: ReceiverTicket["artifacts"][number],
  onProgress: ReceiverProgress,
  onDebug?: ReceiverDebug,
): Promise<ResolvedArtifact> {
  const resolveTarget = artifact.browser_resolve_path || artifact.browser_resolve_url
  onProgress({ phase: "RESOLVING", message: "正在向 P20 请求接收地址：有 QQ Runtime 时会优先直接生成临时直链，否则等待 QQ 公共分享同步…", artifact_name: artifact.name })
  debug(onDebug, "resolve", "开始解析接收地址", { artifact: artifact.name, size: artifact.size })
  for (let attempt = 0; attempt <= QQ_RESOLVE_RETRY_DELAYS_MS.length; attempt += 1) {
    debug(onDebug, "resolve", "请求 receiver-url", { attempt: attempt + 1 })
    const response = await fetch(resolveTarget, { credentials: "same-origin", cache: "no-store" })
    debug(onDebug, "resolve", "receiver-url 响应", { attempt: attempt + 1, status: response.status })
    if (response.ok) {
      const resolved = await response.json() as { url: string; resolver?: string }
      onProgress({
        phase: "RESOLVING",
        message: resolved.resolver === "qq_runtime" ? "QQ Runtime 已生成临时直链，开始接收…" : "QQ 公共分享地址已同步，开始接收…",
        artifact_name: artifact.name,
      })
      let urlHost = "unknown"
      try { urlHost = new URL(resolved.url).host } catch {}
      debug(onDebug, "resolve", "接收地址解析成功", { resolver: resolved.resolver || "unknown", url_host: urlHost })
      return { ...artifact, url: resolved.url }
    }
    if (response.status !== 503) throw new Error(`获取 QQ CDN 地址失败：HTTP ${response.status}`)
    let detail: { message?: string; waited_seconds?: number; retry_after?: number; status?: string } = {}
    try {
      const payload = await response.json() as { detail?: typeof detail }
      detail = payload.detail || {}
    } catch {}
    const message = detail.message || "QQ 文件已经上传完成，但公开下载地址仍在同步。"
    const waited = detail.waited_seconds ?? 0
    if (attempt === QQ_RESOLVE_RETRY_DELAYS_MS.length) {
      throw new Error(`${message} 已等待约 ${waited} 秒。浏览器已停止自动等待；请稍后刷新或重新点击“接收”，无需重新下载或重新上传。`)
    }
    const delayMs = QQ_RESOLVE_RETRY_DELAYS_MS[attempt]
    onProgress({
      phase: "RESOLVING",
      message: `${message} 已等待约 ${waited} 秒；第 ${attempt + 1}/${QQ_RESOLVE_RETRY_DELAYS_MS.length} 次浏览器重试将在 ${delayMs / 1000} 秒后进行。你也可以稍后重新点击接收，任务无需重传。`,
      artifact_name: artifact.name,
    })
    await new Promise((resolve) => window.setTimeout(resolve, delayMs))
  }
  throw new Error("QQ 下载地址暂时不可用")
}

async function resolveArtifacts(ticket: ReceiverTicket, onProgress: ReceiverProgress, onDebug?: ReceiverDebug): Promise<ResolvedArtifact[]> {
  const resolved: ResolvedArtifact[] = []
  for (const artifact of ticket.artifacts) resolved.push(await resolveOneArtifact(artifact, onProgress, onDebug))
  return resolved
}

async function directoryTarget(root: any, filename: string): Promise<any> {
  const safeParts = filename.split("/").filter((part) => part && part !== "." && part !== "..")
  if (!safeParts.length) throw new Error("压缩包包含无效文件名")
  let dir = root
  for (const part of safeParts.slice(0, -1)) dir = await dir.getDirectoryHandle(part, { create: true })
  const file = await dir.getFileHandle(safeParts.at(-1), { create: true })
  return file.createWritable()
}

async function extractZip(artifacts: ResolvedArtifact[], ticket: ReceiverTicket, onProgress: ReceiverProgress, onDebug?: ReceiverDebug) {
  const { BlobReader, BlobWriter, Reader, SplitDataReader, ZipReader } = await import("@zip.js/zip.js")
  const totalBytes = artifacts.reduce((sum, artifact) => sum + artifact.size, 0)
  const coverages = artifacts.map(() => new ByteCoverage())
  const rate = new RateTracker()
  let networkBytes = 0
  let currentEntry: { name: string; index: number; count: number; bytesDone?: number; bytesTotal?: number } | null = null

  const emitDownload = (artifactIndex: number) => {
    const done = coverages.reduce((sum, coverage) => sum + coverage.covered, 0)
    const speed = rate.sample(done)
    const eta = speed && done < totalBytes ? Math.max(0, (totalBytes - done) / speed) : undefined
    const phase: ReceiverPhase = currentEntry ? "DECRYPTING_EXTRACTING" : "DOWNLOADING"
    onProgress({
      phase,
      message: currentEntry ? `正在下载并解密解压：${currentEntry.name}` : `正在从 QQ CDN 下载：${artifacts[artifactIndex].name}`,
      artifact_name: currentEntry?.name || artifacts[artifactIndex].name,
      download_percent: progressPercent(done, totalBytes),
      download_bytes_done: done,
      download_bytes_total: totalBytes,
      speed_bps: speed,
      eta_seconds: eta,
      entry_index: currentEntry?.index,
      entry_count: currentEntry?.count,
      entry_bytes_done: currentEntry?.bytesDone,
      entry_bytes_total: currentEntry?.bytesTotal,
      percent: currentEntry?.bytesTotal ? progressPercent(currentEntry.bytesDone || 0, currentEntry.bytesTotal) : undefined,
      network_bytes: networkBytes,
    })
  }

  class QqRangeReader extends Reader<string> {
    readonly url: string
    readonly expectedSize: number
    readonly artifactIndex: number

    constructor(artifact: ResolvedArtifact, artifactIndex: number) {
      super(artifact.url)
      this.url = artifact.url
      this.expectedSize = artifact.size
      this.artifactIndex = artifactIndex
      this.size = artifact.size
    }

    createReadable(options: { offset?: number; size?: number; chunkSize?: number } = {}): ReadableStream<Uint8Array> {
      return super.createReadable({ ...options, chunkSize: options.chunkSize || 8 * 1024 * 1024 })
    }

    async readUint8Array(index: number, length: number): Promise<Uint8Array> {
      if (length <= 0 || index >= this.expectedSize) return new Uint8Array()
      if (index < 0) throw new Error("QQ CDN Range 起点无效")
      const wanted = Math.min(length, this.expectedSize - index)
      const range = `bytes=${index}-${index + wanted - 1}`
      const started = performance.now()
      let response: Response
      try {
        response = await fetch(this.url, { method: "GET", headers: { Range: range }, referrerPolicy: "no-referrer", cache: "no-store" })
      } catch (error) {
        debug(onDebug, "http", "QQ CDN Range 请求异常", { range, error: error instanceof Error ? error.message : String(error) })
        throw error
      }
      debug(onDebug, "http", "QQ CDN Range 响应", {
        range, status: response.status,
        content_length: response.headers.get("content-length"), content_range: response.headers.get("content-range"),
        last_modified: response.headers.get("last-modified"), etag: response.headers.get("etag"),
        elapsed_ms: Math.round(performance.now() - started),
      })
      if (response.status !== 206) throw new Error(`QQ CDN Range 请求失败：HTTP ${response.status}`)
      const visibleRange = response.headers.get("content-range")
      if (visibleRange) {
        const match = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i.exec(visibleRange.trim())
        if (match) {
          const start = Number(match[1])
          const total = match[3] === "*" ? this.expectedSize : Number(match[3])
          if (start !== index || total !== this.expectedSize) throw new Error("QQ CDN Range 资源大小或偏移不一致")
        }
      }
      const chunks: Uint8Array[] = []
      let received = 0
      if (response.body) {
        const reader = response.body.getReader()
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          if (!value?.length) continue
          chunks.push(value)
          const logicalStart = Math.min(index + received, index + wanted)
          const logicalEnd = Math.min(index + received + value.length, index + wanted)
          coverages[this.artifactIndex].add(logicalStart, logicalEnd)
          received += value.length
          networkBytes += value.length
          emitDownload(this.artifactIndex)
        }
      } else {
        const value = new Uint8Array(await response.arrayBuffer())
        chunks.push(value)
        received = value.length
        networkBytes += value.length
        coverages[this.artifactIndex].add(index, Math.min(index + value.length, index + wanted))
        emitDownload(this.artifactIndex)
      }
      if (received !== wanted) throw new Error(`QQ CDN Range 长度不一致：期望 ${wanted}，实际 ${received}`)
      const data = new Uint8Array(received)
      let offset = 0
      for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length }
      return data
    }
  }

  const rawPicker = (window as any).showDirectoryPicker as undefined | (() => Promise<any>)
  const mobileReceiver = isMobileReceiverBrowser()
  const picker = mobileReceiver ? undefined : rawPicker
  const opfs = (navigator.storage as any)?.getDirectory as undefined | (() => Promise<any>)
  let cachedFiles: File[] | null = null
  let cacheParent: any = null
  let cacheSessionName = ""

  if (opfs) {
    try {
      const storageRoot = await opfs.call(navigator.storage)
      cacheParent = await storageRoot.getDirectoryHandle("p20-receiver-temp", { create: true })
      cacheSessionName = `receive-${ticket.transfer_id}-${Date.now()}`
      const cacheDir = await cacheParent.getDirectoryHandle(cacheSessionName, { create: true })
      cachedFiles = []
      debug(
        onDebug,
        "zip",
        mobileReceiver
          ? "移动端使用浏览器临时文件缓存加密包，下载完成后直接解密并交给浏览器保存"
          : "使用浏览器临时文件缓存加密包，下载完成后再选择保存目录",
        { artifacts: artifacts.length, opfs: true, mobile_receiver: mobileReceiver, directory_picker: Boolean(picker) },
      )
      for (let artifactIndex = 0; artifactIndex < artifacts.length; artifactIndex += 1) {
        const artifact = artifacts[artifactIndex]
        onProgress({
          phase: "DOWNLOADING", message: `正在下载加密包：${artifact.name}`, artifact_name: artifact.name,
          download_percent: progressPercent(coverages.reduce((sum, coverage) => sum + coverage.covered, 0), totalBytes),
          download_bytes_done: coverages.reduce((sum, coverage) => sum + coverage.covered, 0), download_bytes_total: totalBytes,
        })
        const response = await fetch(artifact.url, { referrerPolicy: "no-referrer", cache: "no-store" })
        if (!response.ok) throw new Error(`下载加密包失败：HTTP ${response.status}`)
        const handle = await cacheDir.getFileHandle(`part-${String(artifactIndex).padStart(4, "0")}.bin`, { create: true })
        const writable = await handle.createWritable()
        let received = 0
        try {
          if (response.body) {
            const reader = response.body.getReader()
            while (true) {
              const { done, value } = await reader.read()
              if (done) break
              if (!value?.length) continue
              await writable.write(value)
              const start = received
              received += value.byteLength
              networkBytes += value.byteLength
              coverages[artifactIndex].add(start, Math.min(received, artifact.size))
              emitDownload(artifactIndex)
            }
          } else {
            const value = new Uint8Array(await response.arrayBuffer())
            await writable.write(value)
            received = value.byteLength
            networkBytes += value.byteLength
            coverages[artifactIndex].add(0, Math.min(received, artifact.size))
            emitDownload(artifactIndex)
          }
        } finally {
          await writable.close()
        }
        if (received !== artifact.size) throw new Error(`下载加密包大小不一致：期望 ${artifact.size}，实际 ${received}`)
        const file = await handle.getFile()
        if (file.size !== artifact.size) throw new Error(`浏览器临时文件大小不一致：期望 ${artifact.size}，实际 ${file.size}`)
        cachedFiles.push(file)
      }
    } catch (error) {
      debug(onDebug, "zip", "浏览器临时文件缓存不可用，回退 Range 流式模式", { error: error instanceof Error ? error.message : String(error) })
      if (cacheParent && cacheSessionName) { try { await cacheParent.removeEntry(cacheSessionName, { recursive: true }) } catch {} }
      cachedFiles = null
      cacheParent = null
      cacheSessionName = ""
    }
  }

  let readers: any[]
  if (cachedFiles) {
    readers = cachedFiles.map((file) => new BlobReader(file))
  } else {
    debug(onDebug, "zip", "初始化已知大小 Range Reader", {
      artifacts: artifacts.length, range: true, validator_policy: "ticket_size+range_length+zip_auth",
      ignore_qq_last_modified: true, range_chunk_bytes: 8 * 1024 * 1024,
    })
    readers = artifacts.map((artifact, index) => new QqRangeReader(artifact, index))
  }
  const source = readers.length === 1 ? readers[0] : new SplitDataReader(readers)
  const password = ticket.postprocess?.password || undefined
  const zip = new ZipReader(source, { password, checkAuthenticationCode: true })
  const entries = await zip.getEntries()
  const files = entries.filter((entry) => !entry.directory)
  debug(onDebug, "zip", "ZIP 目录读取成功", { entries: entries.length, files: files.length, cached_before_picker: Boolean(cachedFiles) })
  try {
  let directory: any = null
  if (picker) {
    onProgress({
      phase: "SAVING", message: cachedFiles ? "加密包已下载完成，请选择解压后的保存目录…" : "请选择解压后的保存目录…",
      download_percent: progressPercent(coverages.reduce((sum, coverage) => sum + coverage.covered, 0), totalBytes),
      download_bytes_done: coverages.reduce((sum, coverage) => sum + coverage.covered, 0), download_bytes_total: totalBytes,
    })
    directory = await picker.call(window)
  }
  let extracted = 0
  for (const entry of files) {
    currentEntry = { name: entry.filename, index: extracted + 1, count: files.length }
    const downloadAtEntryStart = coverages.reduce((sum, coverage) => sum + coverage.covered, 0)
    const baseProgress = {
      phase: "DECRYPTING_EXTRACTING" as const,
      message: `正在解密解压：${entry.filename}`,
      artifact_name: entry.filename,
      entry_index: currentEntry.index,
      entry_count: currentEntry.count,
      download_percent: progressPercent(downloadAtEntryStart, totalBytes),
      download_bytes_done: downloadAtEntryStart,
      download_bytes_total: totalBytes,
      network_bytes: networkBytes,
    }
    onProgress(baseProgress)
    debug(onDebug, "extract", "开始解密解压文件", { filename: entry.filename, uncompressed_size: entry.uncompressedSize || 0 })
    const extractOptions = {
      password, checkAuthenticationCode: true,
      onprogress: (progress: number, total: number) => {
        if (currentEntry) { currentEntry.bytesDone = progress; currentEntry.bytesTotal = total }
        const downloadDone = coverages.reduce((sum, coverage) => sum + coverage.covered, 0)
        const speed = rate.sample(downloadDone)
        onProgress({
          ...baseProgress,
          percent: progressPercent(progress, total),
          download_percent: progressPercent(downloadDone, totalBytes),
          download_bytes_done: downloadDone,
          download_bytes_total: totalBytes,
          speed_bps: speed,
          eta_seconds: speed && downloadDone < totalBytes ? Math.max(0, (totalBytes - downloadDone) / speed) : undefined,
          entry_bytes_done: progress,
          entry_bytes_total: total,
          network_bytes: networkBytes,
        })
      },
    }
    if (directory) {
      const writable = await directoryTarget(directory, entry.filename)
      await entry.getData(writable, extractOptions)
      await writable.close?.()
    } else {
      const blob = await entry.getData(new BlobWriter(), extractOptions)
      if (blob) triggerDownload(blob, entry.filename.split("/").at(-1) || "file.bin")
    }
    extracted += 1
    debug(onDebug, "extract", "文件解密解压完成", { filename: entry.filename })
  }
  currentEntry = null
  const downloadDone = coverages.reduce((sum, coverage) => sum + coverage.covered, 0)
  debug(onDebug, "zip", "ZIP 解密解压完成", { extracted, unique_download_bytes: downloadDone, network_bytes: networkBytes })
  if (!extracted) throw new Error("压缩包中没有可提取文件")
  onProgress({
    phase: "WAITING_ACK", message: "解密解压完成；任务仍保留，等待你确认清理。",
    download_percent: progressPercent(downloadDone, totalBytes), download_bytes_done: downloadDone, download_bytes_total: totalBytes,
    entry_index: extracted, entry_count: files.length,
  })
  return { extracted, streamedToDirectory: Boolean(directory) }
  } finally {
    try { await zip.close() } catch {}
    if (cacheParent && cacheSessionName) {
      try { await cacheParent.removeEntry(cacheSessionName, { recursive: true }) } catch {}
    }
  }
}

async function downloadDirectArtifacts(artifacts: ResolvedArtifact[], onProgress: ReceiverProgress, onDebug?: ReceiverDebug) {
  const totalBytes = artifacts.reduce((sum, artifact) => sum + artifact.size, 0)
  const rate = new RateTracker()
  let completedBytes = 0
  for (const artifact of artifacts) {
    debug(onDebug, "download", "开始浏览器直收文件", { filename: artifact.name, size: artifact.size })
    const response = await fetch(artifact.url, { referrerPolicy: "no-referrer", cache: "no-store" })
    if (!response.ok) throw new Error(`下载文件失败：HTTP ${response.status}`)
    let artifactBytes = 0
    const emit = () => {
      const done = Math.min(completedBytes + artifactBytes, totalBytes)
      const speed = rate.sample(done)
      onProgress({
        phase: "DOWNLOADING", message: `正在下载：${artifact.name}`, artifact_name: artifact.name,
        percent: progressPercent(done, totalBytes), bytes_done: done, bytes_total: totalBytes,
        download_percent: progressPercent(done, totalBytes), download_bytes_done: done, download_bytes_total: totalBytes,
        speed_bps: speed, eta_seconds: speed && done < totalBytes ? Math.max(0, (totalBytes - done) / speed) : undefined,
        network_bytes: done,
      })
    }
    emit()
    let blob: Blob
    if (response.body) {
      const observed = response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
        transform(chunk, controller) {
          artifactBytes += chunk.byteLength
          emit()
          controller.enqueue(chunk)
        },
      }))
      blob = await new Response(observed, { headers: { "content-type": response.headers.get("content-type") || "application/octet-stream" } }).blob()
    } else {
      blob = await response.blob()
      artifactBytes = blob.size
      emit()
    }
    if (blob.size !== artifact.size) throw new Error(`下载文件大小不一致：期望 ${artifact.size}，实际 ${blob.size}`)
    onProgress({
      phase: "SAVING", message: `下载完成，正在保存：${artifact.name}`, artifact_name: artifact.name,
      percent: progressPercent(completedBytes + artifactBytes, totalBytes), bytes_done: completedBytes + artifactBytes, bytes_total: totalBytes,
      download_percent: progressPercent(completedBytes + artifactBytes, totalBytes), download_bytes_done: completedBytes + artifactBytes, download_bytes_total: totalBytes,
    })
    triggerDownload(blob, artifact.name)
    completedBytes += artifactBytes
    debug(onDebug, "download", "浏览器直收文件完成", { filename: artifact.name, size: blob.size })
  }
  onProgress({
    phase: "WAITING_ACK", message: "下载已交给浏览器；确认文件保存完成后请点击“已取回，清理”",
    percent: 100, bytes_done: completedBytes, bytes_total: totalBytes,
    download_percent: 100, download_bytes_done: completedBytes, download_bytes_total: totalBytes,
  })
}

export async function receiveInBrowser(
  ticket: ReceiverTicket,
  onProgress: ReceiverProgress,
  onDebug?: ReceiverDebug,
): Promise<{ extracted?: number; downloaded?: number; streamedToDirectory?: boolean }> {
  debug(onDebug, "browser", "Browser Receiver 启动", {
    user_agent: navigator.userAgent,
    language: navigator.language,
    directory_picker: Boolean((window as any).showDirectoryPicker),
    mobile_receiver: isMobileReceiverBrowser(),
    directory_picker_enabled: Boolean((window as any).showDirectoryPicker) && !isMobileReceiverBrowser(),
  })
  const artifacts = await resolveArtifacts(ticket, onProgress, onDebug)
  if (!artifacts.length) throw new Error("Receiver ticket 没有文件")
  const postprocess = ticket.postprocess || { kind: "none" as const }
  if (postprocess.kind === "archive") {
    if (!String(postprocess.format || "").startsWith("zip")) throw new Error("浏览器接收目前只自动处理 ZIP；7z 请使用 p20-receive")
    try {
      return await extractZip(artifacts, ticket, onProgress, onDebug)
    } catch (error) {
      debug(onDebug, "error", "Browser Receiver 解压失败", { error: error instanceof Error ? error.message : String(error), stack: error instanceof Error ? (error.stack || "") : "" })
      throw error
    }
  }
  await downloadDirectArtifacts(artifacts, onProgress, onDebug)
  return { downloaded: artifacts.length }
}
