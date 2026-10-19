const activeJobStates = new Set(['queued', 'resolving_source', 'downloading', 'verifying', 'paused'])

export function selectRecordingJob(jobs, sourceRecordingId) {
  const requestedTime = (job) => {
    const value = job.requested_at
    if (!value) return Number.NEGATIVE_INFINITY
    const parsed = Date.parse(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
    return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY
  }
  return jobs.filter((job) => job.source_recording_id === sourceRecordingId).sort((a, b) => {
    const activeDifference = Number(activeJobStates.has(b.state)) - Number(activeJobStates.has(a.state))
    if (activeDifference) return activeDifference
    const aTime = requestedTime(a); const bTime = requestedTime(b)
    return aTime === bTime ? 0 : aTime > bTime ? -1 : 1
  })[0]
}

export function isEnqueueEligible(recording, job, enqueueReady) {
  return enqueueReady === true
    && recording.archive_status !== 'archived_verified'
    && !activeJobStates.has(job?.state ?? '')
}

export function selectEnqueueCandidates(ids, recordings, jobFor, enqueueReady) {
  const recordingsById = new Map(recordings.map((recording) => [recording.source_recording_id, recording]))
  return [...new Set(ids)].filter((id) => {
    const recording = recordingsById.get(id)
    return recording ? isEnqueueEligible(recording, jobFor(recording), enqueueReady) : false
  })
}

export function playbackResult(access) {
  if (access.status === 'normalization_pending') return { message: '兼容播放副本仍在处理中。' }
  if (access.status !== 'available' || !access.playback_path) return { message: '当前资源没有可播放版本。' }
  return { path: access.playback_path, message: '' }
}
