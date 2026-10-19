import test from 'node:test'
import assert from 'node:assert/strict'
import { selectRecordingJob, isEnqueueEligible } from '../src/archiveBehavior.js'

const job = (id, state, time, source = 'r1') => ({ archive_job_id: id, source_recording_id: source, state, requested_at: time })

test('active retry cannot be hidden by a lexically larger historical job ID', () => {
  for (const state of ['queued', 'paused', 'downloading', 'verifying', 'resolving_source']) {
    const current = job('a-new', state, '2026-10-16T10:00:00')
    const jobs = [job('z-old', 'cancelled', '2026-10-16T09:00:00'), job('zz-old', 'failed', '2026-10-16T09:30:00'), current]
    assert.equal(selectRecordingJob(jobs, 'r1'), current)
    assert.equal(isEnqueueEligible({ source_recording_id: 'r1' }, selectRecordingJob(jobs, 'r1'), true), false)
    assert.deepEqual(jobs.map(j => j.archive_job_id), ['z-old', 'zz-old', 'a-new'])
  }
})

test('terminal history uses requested time, not random ID, and excludes other sources', () => {
  const latest = job('a', 'cancelled', '2026-10-16T10:00:00Z')
  assert.equal(selectRecordingJob([job('z', 'failed', '2026-10-16T09:00:00'), latest, job('zz', 'queued', '2026-10-16T11:00:00', 'other')], 'r1'), latest)
  assert.equal(selectRecordingJob([], 'r1'), undefined)
})

test('active task wins even if an inconsistent newer terminal row is present', () => {
  const active = job('a', 'paused', '2026-10-16T09:00:00')
  assert.equal(selectRecordingJob([job('z', 'cancelled', '2026-10-16T10:00:00'), active], 'r1'), active)
})

test('missing or invalid timestamps do not override a known newer terminal task', () => {
  const latest = job('a', 'completed', '2026-10-16T10:00:00')
  assert.equal(selectRecordingJob([job('z', 'failed', undefined), job('zz', 'cancelled', 'invalid'), latest], 'r1'), latest)
})
