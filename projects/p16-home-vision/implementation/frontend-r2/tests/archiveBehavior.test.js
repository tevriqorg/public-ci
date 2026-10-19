import test from 'node:test'
import assert from 'node:assert/strict'
import { isEnqueueEligible, playbackResult, selectEnqueueCandidates } from '../src/archiveBehavior.js'

test('enqueue eligibility requires ready worker and excludes archived or active recordings', () => {
  const recording = { source_recording_id: 'r1', archive_status: 'pending' }
  assert.equal(isEnqueueEligible(recording, undefined, true), true)
  assert.equal(isEnqueueEligible(recording, undefined, false), false)
  assert.equal(isEnqueueEligible({ ...recording, archive_status: 'archived_verified' }, undefined, true), false)
  for (const state of ['queued', 'resolving_source', 'downloading', 'verifying', 'paused']) {
    assert.equal(isEnqueueEligible(recording, { state }, true), false, state)
  }
})

test('enqueue preview deduplicates and removes missing, archived, active, and non-ready items', () => {
  const recordings = [
    { source_recording_id: 'ready', archive_status: 'available' },
    { source_recording_id: 'archived', archive_status: 'archived_verified' },
    { source_recording_id: 'active', archive_status: 'available' },
  ]
  const jobs = { active: { state: 'downloading' } }
  const jobFor = (recording) => jobs[recording.source_recording_id]
  assert.deepEqual(selectEnqueueCandidates(['ready', 'ready', 'archived', 'active', 'missing'], recordings, jobFor, true), ['ready'])
  assert.deepEqual(selectEnqueueCandidates(['ready'], recordings, jobFor, false), [])
})

test('playback uses only API-reported playback path and treats unavailable states as non-playable', () => {
  assert.deepEqual(playbackResult({ status: 'available', playback_path: '/api/v1/media/id/playback' }), {
    path: '/api/v1/media/id/playback', message: '',
  })
  assert.equal(playbackResult({ status: 'normalization_pending' }).message, '兼容播放副本仍在处理中。')
  assert.equal(playbackResult({ status: 'not_found' }).path, undefined)
  assert.equal(playbackResult({ status: 'available' }).path, undefined)
})
