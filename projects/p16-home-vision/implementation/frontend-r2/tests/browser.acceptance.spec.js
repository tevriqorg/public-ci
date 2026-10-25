import { test, expect } from '@playwright/test'

import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'

const viewports = [
  { name: '1440', width: 1440, height: 900 },
  { name: '1280', width: 1280, height: 800 },
  { name: '834', width: 834, height: 1112 },
  { name: '390', width: 390, height: 844 },
]

const synthetic = {
  asset: {
    asset_id: 'synthetic-asset-1', source_recording_id: 'synthetic-recording-1', camera: '客厅',
    started_at: '2026-10-16T10:00:00Z', verification_state: 'verified', lifecycle_state: 'present',
    size_bytes: 10485760, media_duration_ms: 60000, video_codec: 'hevc',
    stages: { raw: 'done', web: 'done', frames: 'done', audio: 'done', transcript: 'queued', reduce: 'queued', package: 'idle' },
  },
  recording: {
    source_recording_id: 'synthetic-recording-1', track_id: 'cam-1', source_start: '2026-10-16T10:00:00Z',
    source_end: '2026-10-16T10:01:00Z', duration_ms: 60000, source_size_if_known: 10485760,
    source_state: 'available', archive_status: 'pending', thumbnail_available: false,
  },
}

async function installSyntheticApi(page, { archiveJobs = () => [], videoBytes = Buffer.alloc(0), videoContentType = 'video/mp4' } = {}) {
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/') && !url.pathname.startsWith('/internal/')) return route.continue()
    const paged = (items) => ({ items, next_after_id: null })
    let body
    switch (url.pathname) {
      case '/api/v1/assets': body = paged([synthetic.asset]); break
      case '/api/v1/recordings': body = paged([synthetic.recording]); break
      case '/api/v1/scan-runs': body = paged([]); break
      case '/api/v1/archive-jobs': body = paged(archiveJobs()); break
      case '/api/v1/cameras': body = paged([{ track_id: 'cam-1', name: '客厅' }]); break
      case '/api/v1/storage-volumes': body = paged([
        { storage_volume_id: 'A', online: true, writable: true, total_bytes: 1000000000000, free_bytes: 600000000000 },
        { storage_volume_id: 'B', online: true, writable: true, total_bytes: 1000000000000, free_bytes: 700000000000 },
      ]); break
      case '/api/v1/runtime-status': body = { runtime: 'container', runtime_version: 'synthetic', cpu: { load_1m: 0.2, logical_cores: 8 }, memory: { used_bytes: 1000000000, limit_bytes: 8000000000 }, host: { status: 'not_collected', reason: 'synthetic fixture' } }; break
      case '/api/v1/analysis-runs': body = paged([]); break
      case '/api/v1/events': body = paged([]); break
      case '/api/v1/review-flags': body = paged([]); break
      case '/api/v1/audit-events': body = paged([]); break
      case '/internal/archive/status': body = { configured: true, enqueue_ready: true, control_actions: ['pause_queued', 'cancel_waiting', 'resume_paused', 'retry_failed'], heartbeat: { status: 'healthy' } }; break
      case '/api/v1/media/synthetic-asset-1/access': body = { status: 'available', playback_path: '/api/r2/assets/synthetic-asset-1/web' }; break
      default:
        if (url.pathname === '/internal/archive/batch' && route.request().method() === 'POST') body = { queued_count: 1 }
        else if (url.pathname.startsWith('/internal/archive/jobs/') && route.request().method() === 'POST') body = { ok: true }
        else if (url.pathname === '/api/r2/assets/synthetic-asset-1/web') {
          const range = route.request().headers().range
          const requested = range?.match(/^bytes=(\d+)-(\d*)$/)
          if (requested) {
            const start = Number(requested[1])
            const end = Math.min(requested[2] ? Number(requested[2]) : videoBytes.length - 1, videoBytes.length - 1)
            return route.fulfill({ status: 206, contentType: videoContentType, body: videoBytes.subarray(start, end + 1),
              headers: { 'Accept-Ranges': 'bytes', 'Content-Range': `bytes ${start}-${end}/${videoBytes.length}` } })
          }
          return route.fulfill({ status: 200, contentType: videoContentType, body: videoBytes,
            headers: { 'Accept-Ranges': 'bytes' } })
        }
        else return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'synthetic route not configured' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
}

for (const viewport of viewports) {
  test(`synthetic product browser acceptance ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await installSyntheticApi(page)
    await page.goto('/')

    await expect(page.getByRole('heading', { name: '总览', exact: true })).toBeVisible()
    const nav = page.getByRole('navigation', { name: '主导航' })
    for (const label of ['总览', '归档', '分析', '任务', '待复核', '记录']) {
      await expect(nav.locator('button').filter({ hasText: label })).toBeVisible()
    }
    await expect(page.locator('.calendar-panel')).toBeVisible()

    await nav.locator('button').filter({ hasText: '归档' }).click()
    await expect(page.getByRole('heading', { name: '归档', exact: true })).toBeVisible()
    await expect(page.locator('.camera-lane')).toBeVisible()
    await page.locator('input[type="checkbox"]').check()
    await page.getByRole('button', { name: '批量预览' }).click()
    await expect(page.getByRole('dialog', { name: '确认归档队列' })).toBeVisible()
    await page.getByRole('button', { name: '返回修改' }).click()

    for (const label of ['分析', '任务', '待复核', '记录']) {
      await nav.locator('button').filter({ hasText: label }).click()
      await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible()
    }

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow).toBeLessThanOrEqual(2)
  })
}

test('@S024 six pages retain backend task truth without document reload', async ({ page }, testInfo) => {
  let jobRequests = 0
  let documentRequests = 0
  let stateIndex = 0
  const queued = {
    archive_job_id: 'synthetic-s024-job', source_recording_id: 'synthetic-recording-1',
    state: 'queued', progress_percent: 68, bytes_received: 680, source_size_bytes: 1000,
  }
  const downloading = { ...queued, state: 'downloading', progress_percent: 69, bytes_received: 690 }
  await page.context().tracing.start({ screenshots: true, snapshots: true, sources: true })
  try {
    page.on('request', (request) => {
      if (request.resourceType() === 'document') documentRequests += 1
    })
    await installSyntheticApi(page, {
      archiveJobs: () => {
        jobRequests += 1
        return [stateIndex === 0 ? queued : downloading]
      },
    })
    await page.goto('/')
    const documentTime = await page.evaluate(() => performance.timeOrigin)
    const nav = page.getByRole('navigation', { name: '主导航' })
    const labels = ['总览', '归档', '分析', '任务', '待复核', '记录']

    for (const label of labels) {
      await nav.locator('button').filter({ hasText: label }).click()
      await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible()
      expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentTime)
    }

    await nav.locator('button').filter({ hasText: '任务' }).click()
    await expect(page.getByRole('heading', { name: '任务', exact: true })).toBeVisible()
    await expect.poll(() => jobRequests).toBeGreaterThanOrEqual(1)
    const task = page.locator('.task-row').filter({ hasText: 'synthetic-s024-job' })
    await expect(task).toBeVisible()
    await expect(task).toContainText('等待中')
    await expect(task).toContainText('68%')
    const requestsBeforeRefresh = jobRequests
    stateIndex = 1
    const refreshResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/v1/archive-jobs')
    await page.getByRole('button', { name: '刷新任务' }).click()
    const response = await refreshResponse
    expect((await response.json()).items[0].state).toBe('downloading')
    expect(jobRequests).toBeGreaterThan(requestsBeforeRefresh)
    await expect(task).toContainText('拉取中')
    await expect(task).toContainText('69%')
    expect(jobRequests).toBeGreaterThanOrEqual(2)
    expect(documentRequests).toBe(1)
    expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentTime)
    await page.screenshot({ path: testInfo.outputPath('s024-six-pages-task-update.png'), fullPage: true })
  } finally {
    await page.context().tracing.stop({ path: testInfo.outputPath('s024-browser-trace.zip') })
  }
})

test('@S025 player stays active while task progress and worker error update', async ({ page }, testInfo) => {
  let jobRequests = 0
  let documentRequests = 0
  let stateIndex = 0
  const videoPath = testInfo.outputPath('s025-synthetic.mp4')
  const encoded = spawnSync('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
    'color=c=blue:s=64x64:r=10:d=20', '-an', '-c:v', 'libvpx', '-deadline', 'realtime',
    '-cpu-used', '8', '-b:v', '100k', '-f', 'webm', videoPath,
  ])
  expect(encoded.status, encoded.stderr.toString()).toBe(0)
  const videoBytes = readFileSync(videoPath)
  const states = [
    { archive_job_id: 'synthetic-s025-job', source_recording_id: 'synthetic-recording-1', state: 'downloading', progress_percent: 68, bytes_received: 680, source_size_bytes: 1000 },
    { archive_job_id: 'synthetic-s025-job', source_recording_id: 'synthetic-recording-1', state: 'downloading', progress_percent: 69, bytes_received: 690, source_size_bytes: 1000 },
    { archive_job_id: 'synthetic-s025-job', source_recording_id: 'synthetic-recording-1', state: 'failed', progress_percent: 69, bytes_received: 690, source_size_bytes: 1000, error_code: 'SYNTHETIC_WORKER_FAILURE' },
  ]
  page.on('request', (request) => {
    if (request.resourceType() === 'document') documentRequests += 1
  })
  await installSyntheticApi(page, {
    videoBytes,
    videoContentType: 'video/webm',
    archiveJobs: () => {
      jobRequests += 1
      return [states[stateIndex]]
    },
  })
  await page.goto('/')
  const documentTime = await page.evaluate(() => performance.timeOrigin)
  const nav = page.getByRole('navigation', { name: '主导航' })
  await nav.locator('button').filter({ hasText: '归档' }).click()
  await expect(page.getByRole('heading', { name: '归档', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '播放', exact: true }).click()

  const player = page.getByRole('complementary', { name: '浮动播放器' })
  const video = player.locator('video')
  await expect(video).toBeVisible()
  await expect(video).toHaveAttribute('src', '/api/r2/assets/synthetic-asset-1/web')
  await video.evaluate(async (element) => {
    element.muted = true
    await element.play()
  })
  await expect.poll(() => video.evaluate((element) => !element.paused && element.currentTime > 0)).toBe(true)
  const initialPlaybackTime = await video.evaluate((element) => element.currentTime)

  await nav.locator('button').filter({ hasText: '任务' }).click()
  await expect(page.getByRole('heading', { name: '任务', exact: true })).toBeVisible()
  const task = page.locator('.task-row').filter({ hasText: 'synthetic-s025-job' })
  await expect(task).toBeVisible()
  await expect(task).toContainText('68%')
  const beforeProgressRefresh = jobRequests
  stateIndex = 1
  await page.getByRole('button', { name: '刷新任务' }).click()
  await expect.poll(() => jobRequests).toBeGreaterThan(beforeProgressRefresh)
  await expect(task).toContainText('69%')
  await expect.poll(() => video.evaluate((element) => element.isConnected && !element.paused
    && element.currentTime > initialPlaybackTime)).toBe(true)

  const playbackAfterProgress = await video.evaluate((element) => element.currentTime)
  const beforeFailureRefresh = jobRequests
  stateIndex = 2
  await page.getByRole('button', { name: '刷新任务' }).click()
  await expect.poll(() => jobRequests).toBeGreaterThan(beforeFailureRefresh)
  await expect(task).toContainText('SYNTHETIC_WORKER_FAILURE')
  await expect(task).toContainText('69%')
  await expect.poll(() => video.evaluate((element) => element.isConnected && !element.paused
    && element.currentTime > playbackAfterProgress)).toBe(true)
  expect(jobRequests).toBeGreaterThanOrEqual(3)
  expect(documentRequests).toBe(1)
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentTime)
})
