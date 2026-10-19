export type ArchiveRecording = { source_recording_id: string; archive_status?: string }
export type ArchiveJob = { state?: string } | undefined
export type RecordingJob = { source_recording_id: string; state: string; requested_at?: string }
export type PlaybackAccess = { status?: string; playback_path?: string }
export type PlaybackResult = { path?: string; message: string }
export function selectRecordingJob<T extends RecordingJob>(jobs: T[], sourceRecordingId: string): T | undefined
export function isEnqueueEligible(recording: ArchiveRecording, job: ArchiveJob, enqueueReady: boolean): boolean
export function selectEnqueueCandidates(ids: string[], recordings: ArchiveRecording[], jobFor: (recording: ArchiveRecording) => ArchiveJob, enqueueReady: boolean): string[]
export function playbackResult(access: PlaybackAccess): PlaybackResult
