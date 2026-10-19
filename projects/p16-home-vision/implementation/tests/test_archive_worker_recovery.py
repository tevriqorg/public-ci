from datetime import datetime, timezone
from pathlib import Path

from app.infra.media.ffprobe import MediaProbeResult
from app.models import ArchiveJob, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume
from scripts import archive_worker


class FakeProbe:
    def probe(self, path: Path) -> MediaProbeResult:
        return MediaProbeResult(
            duration_ms=10_000, has_video=True, container="mp4",
            video_codec="h264", audio_codec="aac", width=1920, height=1080,
        )


class NoNetworkClient:
    def search_history(self, _request):
        raise AssertionError("orphan recovery must not query NVR")

    def download_to_part(self, *_args, **_kwargs):
        raise AssertionError("orphan recovery must not redownload source media")


def test_worker_adopts_valid_promoted_orphan_before_nvr_access(session, tmp_path, monkeypatch):
    nvr = NvrDevice(id="nvr-orphan", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-orphan", nvr_id=nvr.id, track_id="201", camera_name="Cam")
    source = SourceRecording(
        id="source-orphan", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 27, 6, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_size_if_known=4096, source_state="available",
    )
    volume = StorageVolume(
        id="volume-orphan", node_id="fixture", mount_path_runtime=str(tmp_path),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    )
    job = ArchiveJob(id="job-orphan", source_recording_id=source.id, state="queued")
    session.add_all([nvr, camera])
    session.flush()
    session.add(source)
    session.flush()
    session.add_all([volume, job])
    session.commit()

    final_path = (
        tmp_path / "recordings/2026/08/27/camera-201/060000-source-orphan.mp4"
    )
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"VIDEO" + b"x" * 4091)
    monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)

    assert archive_worker.process_one(
        session, NoNetworkClient(), probe=FakeProbe(), worker_id="fixture-worker"
    ) == job.id
    assert session.get(ArchiveJob, job.id).state == "completed"
    assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 1
    assert final_path.exists()
