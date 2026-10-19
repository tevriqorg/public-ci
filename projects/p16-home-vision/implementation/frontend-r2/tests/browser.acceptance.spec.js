import { test, expect } from '@playwright/test'

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

async function installSyntheticApi(page) {
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/') && !url.pathname.startsWith('/internal/')) return route.continue()
    const paged = (items) => ({ items, next_after_id: null })
    let body
    switch (url.pathname) {
      case '/api/v1/assets': body = paged([synthetic.asset]); break
      case '/api/v1/recordings': body = paged([synthetic.recording]); break
      case '/api/v1/scan-runs': body = paged([]); break
      case '/api/v1/archive-jobs': body = paged([]); break
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
        else if (url.pathname === '/api/r2/assets/synthetic-asset-1/web') return route.fulfill({ status: 206, contentType: 'video/mp4', body: '' })
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
