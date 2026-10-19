from datetime import datetime, timedelta, timezone

from app.domain.archive_queue import (
    claim_next_archive_job,
    enqueue_archive_jobs,
    recover_interrupted_archive_jobs,
    renew_archive_claim,
    retry_due_archive_jobs,
    retry_failed_archive_jobs,
    set_queued_jobs_state,
)
from app.models import ArchiveJob, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume


def test_claim_prioritizes_oldest_source_recording(session) -> None:
    nvr = NvrDevice(id="nvr-order", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-order", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    session.add(nvr); session.flush(); session.add(camera); session.flush()
    newer = SourceRecording(id="source-newer", nvr_id=nvr.id, track_id=camera.track_id, source_start=datetime(2026, 7, 20, tzinfo=timezone.utc), source_end=datetime(2026, 7, 20, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    older = SourceRecording(id="source-older", nvr_id=nvr.id, track_id=camera.track_id, source_start=datetime(2026, 7, 10, tzinfo=timezone.utc), source_end=datetime(2026, 7, 10, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    session.add_all([newer, older])
    session.flush()
    session.add_all([ArchiveJob(id="job-newer", source_recording_id=newer.id, state="queued"), ArchiveJob(id="job-older", source_recording_id=older.id, state="queued")])
    session.flush()
    claimed = claim_next_archive_job(session, worker_id="worker-a")
    assert claimed is not None
    assert claimed.source_recording_id == older.id
    assert claimed.state == "resolving_source"
    assert claimed.worker_id == "worker-a"
    assert claimed.lease_token
    assert claimed.lease_expires_at is not None


def test_claim_rotates_across_cameras_before_draining_one_camera(session) -> None:
    nvr = NvrDevice(id="nvr-fair", name="NVR", host="fixture")
    cam_a = CameraTrack(id="camera-a", nvr_id=nvr.id, track_id="101", camera_name="A")
    cam_b = CameraTrack(id="camera-b", nvr_id=nvr.id, track_id="102", camera_name="B")
    session.add(nvr)
    session.flush()
    session.add_all([cam_a, cam_b])
    session.flush()
    older_a = SourceRecording(
        id="source-a-old", nvr_id=nvr.id, track_id=cam_a.track_id,
        source_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        source_end=datetime(2026, 7, 1, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_state="available",
    )
    older_b = SourceRecording(
        id="source-b-old", nvr_id=nvr.id, track_id=cam_b.track_id,
        source_start=datetime(2026, 7, 2, tzinfo=timezone.utc),
        source_end=datetime(2026, 7, 2, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_state="available",
    )
    newer_a = SourceRecording(
        id="source-a-new", nvr_id=nvr.id, track_id=cam_a.track_id,
        source_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 1, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_state="available",
    )
    session.add_all([older_a, older_b, newer_a])
    session.flush()
    session.add_all([
        ArchiveJob(id="job-a-old", source_recording_id=older_a.id, state="queued"),
        ArchiveJob(id="job-b-old", source_recording_id=older_b.id, state="queued"),
        ArchiveJob(id="job-a-new", source_recording_id=newer_a.id, state="queued"),
        ArchiveJob(
            id="job-a-prior", source_recording_id=newer_a.id, state="completed",
            started_at=datetime.now(timezone.utc),
        ),
    ])
    session.flush()

    claimed = claim_next_archive_job(session, worker_id="worker-fair")
    assert claimed is not None
    assert claimed.source_recording_id == older_b.id


def test_archive_claim_is_renewable_and_only_stale_claims_recover(session) -> None:
    nvr = NvrDevice(id="nvr-lease", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-lease", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(
        id="source-lease", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        source_end=datetime(2026, 9, 1, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10000, source_state="available",
    )
    session.add(nvr); session.flush()
    session.add(camera); session.flush()
    session.add(source); session.flush()
    session.add(ArchiveJob(id="job-live", source_recording_id=source.id, state="queued"))
    session.flush()
    claimed = claim_next_archive_job(session, worker_id="worker-a")
    assert claimed is not None
    token = claimed.lease_token
    assert token
    assert renew_archive_claim(session, claimed.id, "worker-a", token, "downloading")
    session.refresh(claimed)
    assert claimed.lease_expires_at is not None
    assert recover_interrupted_archive_jobs(session) == 0

    stale_source = SourceRecording(
        id="source-stale", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 9, 2, tzinfo=timezone.utc),
        source_end=datetime(2026, 9, 2, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_state="available",
    )
    session.add(stale_source)
    session.flush()
    session.add(ArchiveJob(id="job-stale", source_recording_id=stale_source.id, state="downloading", worker_id="dead", lease_token="old", lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    session.flush()
    stale = session.get(ArchiveJob, "job-stale")
    stale.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()
    assert recover_interrupted_archive_jobs(session) == 1
    assert stale.state == "queued"
    assert stale.lease_token is None


def test_transient_archive_failure_retries_after_bounded_backoff(session) -> None:
    nvr = NvrDevice(id="nvr-retry-auto", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-retry-auto", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(
        id="source-retry-auto", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 8, 31, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 31, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_state="available",
    )
    failed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
    job = ArchiveJob(
        id="job-retry-auto", source_recording_id=source.id, state="failed",
        attempt_count=1, error_code="NVR_DOWNLOAD_FAILED", completed_at=failed_at,
    )
    session.add(nvr); session.flush(); session.add(camera); session.flush()
    session.add(source); session.flush()
    session.add(job); session.flush()

    assert retry_due_archive_jobs(
        session, now=failed_at + timedelta(minutes=4), base_delay_seconds=300
    ) == 0
    assert job.state == "failed"
    assert retry_due_archive_jobs(
        session, now=failed_at + timedelta(minutes=5), base_delay_seconds=300
    ) == 1
    assert job.state == "queued"
    assert job.error_code is None
    assert session.query(ArchiveJob).filter_by(id=job.id).count() == 1


def test_enqueue_is_idempotent_and_records_audit(session) -> None:
    nvr = NvrDevice(id="nvr-queue", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-queue", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(id="source-queue", nvr_id=nvr.id, track_id=camera.track_id, source_start=datetime(2026, 8, 31, 0, tzinfo=timezone.utc), source_end=datetime(2026, 8, 31, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    session.add(nvr); session.flush(); session.add(camera); session.flush(); session.add(source); session.flush()
    first = enqueue_archive_jobs(session, [source.id])
    second = enqueue_archive_jobs(session, [source.id])
    assert len(first.queued) == 1
    assert second.queued == ()
    assert second.already_queued == (source.id,)
    assert session.query(ArchiveJob).count() == 1


def test_normalized_asset_does_not_block_missing_source_archive(session, tmp_path) -> None:
    nvr = NvrDevice(id="nvr-normalized", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-normalized", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(id="source-normalized", nvr_id=nvr.id, track_id=camera.track_id, source_start=datetime(2026, 9, 1, tzinfo=timezone.utc), source_end=datetime(2026, 9, 1, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    volume = StorageVolume(id="archive-a", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True)
    session.add(nvr); session.flush(); session.add(camera); session.flush(); session.add_all([source, volume]); session.flush()
    session.add(LocalAsset(id="normalized-only", source_recording_id=source.id, asset_role="normalized_playback", storage_volume_id=volume.id, relative_path="normalized.mp4", verification_state="verified", lifecycle_state="present"))
    session.flush()
    result = enqueue_archive_jobs(session, [source.id])
    assert len(result.queued) == 1


def test_queue_controls_only_change_waiting_jobs_and_retry_keeps_failure_history(session) -> None:
    nvr = NvrDevice(id="nvr-control", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-control", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(id="source-control", nvr_id=nvr.id, track_id=camera.track_id, source_start=datetime(2026, 8, 31, 0, tzinfo=timezone.utc), source_end=datetime(2026, 8, 31, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    session.add(nvr); session.flush(); session.add(camera); session.flush(); session.add(source); session.flush()
    queued = enqueue_archive_jobs(session, [source.id]).queued[0]
    changed, active = set_queued_jobs_state(session, from_states={"queued"}, to_state="paused", event_type="archive_paused")
    assert (changed, active) == (1, 0)
    assert session.get(ArchiveJob, queued).state == "paused"
    changed, _ = set_queued_jobs_state(session, from_states={"paused"}, to_state="queued", event_type="archive_resumed")
    assert changed == 1
    session.get(ArchiveJob, queued).state = "failed"
    session.get(ArchiveJob, queued).error_code = "NVR_DOWNLOAD_FAILED"
    session.flush()
    retry = retry_failed_archive_jobs(session)
    assert len(retry.queued) == 1
    assert retry.queued[0] != queued
    assert session.get(ArchiveJob, queued).state == "failed"
    assert session.get(ArchiveJob, retry.queued[0]).state == "queued"


def test_retry_marks_missing_nvr_source_terminal_without_requeue(session) -> None:
    nvr = NvrDevice(id="nvr-expired", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-expired", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(id="source-expired", nvr_id=nvr.id, track_id=camera.track_id, source_start=datetime(2026, 8, 31, 0, tzinfo=timezone.utc), source_end=datetime(2026, 8, 31, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    session.add(nvr); session.flush(); session.add(camera); session.flush(); session.add(source); session.flush()
    session.add(ArchiveJob(id="job-expired", source_recording_id=source.id, state="failed", error_code="NVR_SOURCE_SEGMENT_NOT_FOUND")); session.flush()
    retry = retry_failed_archive_jobs(session)
    assert retry.queued == ()
    assert source.source_state == "expired"
    assert source.expired_observed_at is not None


def test_expired_source_can_be_queued_for_revalidation_without_rewriting_source_truth(session) -> None:
    nvr = NvrDevice(id="nvr-revalidate", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-revalidate", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    expired_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
    source = SourceRecording(id="source-revalidate", nvr_id=nvr.id, track_id=camera.track_id, source_start=datetime(2026, 8, 31, 0, tzinfo=timezone.utc), source_end=datetime(2026, 8, 31, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="expired", expired_observed_at=expired_at)
    session.add(nvr); session.flush(); session.add(camera); session.flush(); session.add(source); session.flush()
    result = enqueue_archive_jobs(session, [source.id], allow_expired_revalidation=True)
    assert len(result.queued) == 1
    assert source.source_state == "expired"
    assert source.expired_observed_at == expired_at
    assert session.get(ArchiveJob, result.queued[0]).state == "queued"


def test_retry_can_be_scoped_to_one_month(session) -> None:
    nvr = NvrDevice(id="nvr-month", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-month", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    session.add(nvr); session.flush(); session.add(camera); session.flush()
    july = SourceRecording(id="source-july", nvr_id=nvr.id, track_id="101", source_start=datetime(2026, 7, 1, tzinfo=timezone.utc), source_end=datetime(2026, 7, 1, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    august = SourceRecording(id="source-august", nvr_id=nvr.id, track_id="101", source_start=datetime(2026, 8, 1, tzinfo=timezone.utc), source_end=datetime(2026, 8, 1, 0, 0, 10, tzinfo=timezone.utc), duration_ms=10000, source_state="available")
    session.add_all([july, august]); session.flush()
    session.add_all([ArchiveJob(id="job-july", source_recording_id=july.id, state="failed", error_code="NVR_DOWNLOAD_FAILED"), ArchiveJob(id="job-august", source_recording_id=august.id, state="failed", error_code="NVR_DOWNLOAD_FAILED")]); session.flush()
    retry = retry_failed_archive_jobs(session, month_prefix="2026-07")
    assert len(retry.queued) == 1
    assert session.query(ArchiveJob).filter(ArchiveJob.source_recording_id == august.id, ArchiveJob.state == "queued").count() == 0
