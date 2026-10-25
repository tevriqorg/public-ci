from alembic import command
from alembic.config import Config
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.models import ArchiveJob, CameraTrack, NvrDevice, SourceRecording


def _config_and_seed_v5(db_path, *, duplicate_active=False):
    implementation_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(implementation_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(implementation_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0005_normalization_error_code")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        nvr = NvrDevice(id="migration-nvr", name="Migration fixture", host="fixture")
        camera = CameraTrack(
            id="migration-camera", nvr_id=nvr.id, track_id="migration-track", camera_name="Fixture",
        )
        source = SourceRecording(
            id="migration-source", nvr_id=nvr.id, track_id=camera.track_id,
            source_start=datetime(2026, 10, 1), source_end=datetime(2026, 10, 1, 0, 0, 5),
            duration_ms=5000, locator_fingerprint="synthetic-fingerprint", source_state="available",
        )
        session.add(nvr); session.flush(); session.add(camera); session.flush()
        session.add(source); session.flush()
        session.add(ArchiveJob(id="migration-job-a", source_recording_id=source.id, state="queued"))
        if duplicate_active:
            session.add(ArchiveJob(id="migration-job-b", source_recording_id=source.id, state="queued"))
        session.commit()
    return cfg, engine


def test_alembic_upgrade_and_downgrade(tmp_path) -> None:
    db_path = tmp_path / "migration.db"
    implementation_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(implementation_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(implementation_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert {
        "source_recordings", "local_assets", "analysis_profiles", "analysis_runs",
        "archive_source_snapshots",
    } <= tables
    columns = {column["name"] for column in inspect(engine).get_columns("archive_jobs")}
    assert {"worker_id", "lease_token", "lease_expires_at", "lease_phase"} <= columns
    engine.dispose()
    command.downgrade(cfg, "base")
    engine = create_engine(f"sqlite:///{db_path}")
    assert inspect(engine).get_table_names() == ["alembic_version"] or inspect(engine).get_table_names() == []
    engine.dispose()


def test_active_job_index_migration_preserves_canonical_rows(tmp_path):
    cfg, engine = _config_and_seed_v5(tmp_path / "migration-preserve.db")
    command.upgrade(cfg, "head")
    with Session(engine) as session:
        source = session.get(SourceRecording, "migration-source")
        jobs = list(session.scalars(select(ArchiveJob).where(
            ArchiveJob.source_recording_id == "migration-source")))
        assert source.locator_fingerprint == "synthetic-fingerprint"
        assert source.source_state == "available"
        assert [(job.id, job.state) for job in jobs] == [("migration-job-a", "queued")]
        assert "uq_archive_jobs_one_active_per_source" in {
            index["name"] for index in inspect(engine).get_indexes("archive_jobs")
        }
    engine.dispose()


def test_active_job_index_migration_refuses_duplicates_without_deleting_rows(tmp_path):
    cfg, engine = _config_and_seed_v5(tmp_path / "migration-duplicates.db", duplicate_active=True)
    with pytest.raises(RuntimeError, match="active archive-job duplicates"):
        command.upgrade(cfg, "head")
    with Session(engine) as session:
        jobs = list(session.scalars(select(ArchiveJob).where(
            ArchiveJob.source_recording_id == "migration-source").order_by(ArchiveJob.id)))
        assert [(job.id, job.state) for job in jobs] == [
            ("migration-job-a", "queued"), ("migration-job-b", "queued"),
        ]
        assert session.get(SourceRecording, "migration-source").locator_fingerprint == "synthetic-fingerprint"
    engine.dispose()
