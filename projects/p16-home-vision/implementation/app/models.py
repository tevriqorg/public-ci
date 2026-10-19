from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NvrDevice(Base):
    __tablename__ = "nvr_devices"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(255))
    firmware: Mapped[str | None] = mapped_column(String(255))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=80)
    protocol: Mapped[str] = mapped_column(String(32), default="http")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CameraTrack(Base):
    __tablename__ = "camera_tracks"
    __table_args__ = (UniqueConstraint("nvr_id", "track_id", name="uq_camera_track_identity"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    nvr_id: Mapped[str] = mapped_column(ForeignKey("nvr_devices.id"), index=True)
    track_id: Mapped[str] = mapped_column(String(64))
    input_channel: Mapped[str | None] = mapped_column(String(64))
    stream_role: Mapped[str] = mapped_column(String(32), default="main")
    camera_name: Mapped[str] = mapped_column(String(255))
    source_kind: Mapped[str] = mapped_column(String(64), default="hikvision_isapi")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ScanRun(Base):
    __tablename__ = "scan_runs"
    __table_args__ = (
        CheckConstraint("state IN ('running','success','partial','failed')", name="ck_scan_state"),
        ForeignKeyConstraint(["nvr_id", "track_id"], ["camera_tracks.nvr_id", "camera_tracks.track_id"]),
        Index("ix_scan_range", "nvr_id", "track_id", "query_start", "query_end"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    nvr_id: Mapped[str] = mapped_column(String(128))
    track_id: Mapped[str] = mapped_column(String(64))
    query_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    query_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), default="running")
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    response_hash: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceRecording(Base):
    __tablename__ = "source_recordings"
    __table_args__ = (
        UniqueConstraint("nvr_id", "track_id", "source_start", "source_end", name="uq_source_identity"),
        CheckConstraint("source_state IN ('available','expired','unknown')", name="ck_source_state"),
        ForeignKeyConstraint(["nvr_id", "track_id"], ["camera_tracks.nvr_id", "camera_tracks.track_id"]),
        Index("ix_source_track_start", "nvr_id", "track_id", "source_start"),
        Index("ix_source_state_start", "source_state", "source_start"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    nvr_id: Mapped[str] = mapped_column(String(128))
    track_id: Mapped[str] = mapped_column(String(64))
    source_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(BigInteger)
    locator_fingerprint: Mapped[str | None] = mapped_column(String(128))
    source_size_if_known: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(String(128))
    recording_type: Mapped[str | None] = mapped_column(String(128))
    source_state: Mapped[str] = mapped_column(String(32), default="available")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_scan_id: Mapped[str | None] = mapped_column(ForeignKey("scan_runs.id"))
    last_scan_id: Mapped[str | None] = mapped_column(ForeignKey("scan_runs.id"))
    raw_metadata_hash: Mapped[str | None] = mapped_column(String(128))


class ArchiveJob(Base):
    __tablename__ = "archive_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('queued','resolving_source','downloading','verifying','completed','failed','cancelled','paused')", name="ck_archive_job_state"),
        Index("ix_archive_job_state_requested", "state", "requested_at"),
        Index(
            "uq_archive_jobs_one_active_per_source",
            "source_recording_id",
            unique=True,
            sqlite_where=text("state IN ('queued','resolving_source','downloading','verifying','paused')"),
            postgresql_where=text("state IN ('queued','resolving_source','downloading','verifying','paused')"),
        ),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_recording_id: Mapped[str] = mapped_column(ForeignKey("source_recordings.id"), index=True)
    requested_by: Mapped[str] = mapped_column(String(128), default="human")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    temp_path: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail_redacted: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_phase: Mapped[str | None] = mapped_column(String(32))


class StorageVolume(Base):
    __tablename__ = "storage_volumes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(128))
    physical_device_id: Mapped[str | None] = mapped_column(String(255))
    mount_path_runtime: Mapped[str] = mapped_column(Text)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    writable: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveSourceSnapshot(Base):
    __tablename__ = "archive_source_snapshots"
    __table_args__ = (
        Index("ix_archive_snapshot_source", "source_recording_id"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_recording_id: Mapped[str] = mapped_column(ForeignKey("source_recordings.id"), index=True)
    nvr_id: Mapped[str] = mapped_column(String(128))
    track_id: Mapped[str] = mapped_column(String(64))
    source_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(BigInteger)
    locator_fingerprint: Mapped[str | None] = mapped_column(String(128))
    source_size_if_known: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LocalAsset(Base):
    __tablename__ = "local_assets"
    __table_args__ = (
        CheckConstraint("verification_state IN ('pending','verified','failed')", name="ck_asset_verification_state"),
        CheckConstraint("lifecycle_state IN ('present','missing','deleting','deleted')", name="ck_asset_lifecycle_state"),
        Index("ix_asset_source", "source_recording_id"),
        Index("ix_asset_lifecycle", "lifecycle_state"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_recording_id: Mapped[str] = mapped_column(ForeignKey("source_recordings.id"))
    archive_source_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("archive_source_snapshots.id"), index=True)
    asset_role: Mapped[str] = mapped_column(String(64), default="source_archive")
    storage_volume_id: Mapped[str] = mapped_column(ForeignKey("storage_volumes.id"))
    relative_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    container: Mapped[str | None] = mapped_column(String(64))
    video_codec: Mapped[str | None] = mapped_column(String(64))
    audio_codec: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    media_duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    verification_state: Mapped[str] = mapped_column(String(32), default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_state: Mapped[str] = mapped_column(String(32), default="present")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    normalization_worker_id: Mapped[str | None] = mapped_column(String(128), index=True)
    normalization_lease_token: Mapped[str | None] = mapped_column(String(64))
    normalization_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    normalization_phase: Mapped[str | None] = mapped_column(String(32))
    normalization_error_code: Mapped[str | None] = mapped_column(String(128))
    normalization_attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class DeleteIntent(Base):
    __tablename__ = "delete_intents"
    __table_args__ = (CheckConstraint("state IN ('previewed','confirmed','deleting','completed','failed','cancelled')", name="ck_delete_intent_state"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    local_asset_id: Mapped[str] = mapped_column(ForeignKey("local_assets.id"))
    source_recording_id: Mapped[str] = mapped_column(ForeignKey("source_recordings.id"))
    requested_by: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    state: Mapped[str] = mapped_column(String(32), default="previewed")
    reason: Mapped[str | None] = mapped_column(Text)
    expected_relative_path: Mapped[str] = mapped_column(Text)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    expected_size: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(128))


class Tombstone(Base):
    __tablename__ = "tombstones"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_recording_id: Mapped[str] = mapped_column(ForeignKey("source_recordings.id"), index=True)
    local_asset_id: Mapped[str | None] = mapped_column(ForeignKey("local_assets.id"))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    previous_relative_path: Mapped[str | None] = mapped_column(Text)
    previous_size: Mapped[int | None] = mapped_column(BigInteger)
    previous_sha256: Mapped[str | None] = mapped_column(String(64))
    suppress_redownload: Mapped[bool] = mapped_column(Boolean, default=True)


class ReviewFlag(Base):
    __tablename__ = "review_flags"
    __table_args__ = (
        CheckConstraint("review_state IN ('pending','accepted','rejected')", name="ck_review_state"),
        Index("ix_review_state_type", "review_state", "flag_type"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("local_assets.id"))
    flag_type: Mapped[str] = mapped_column(String(128))
    confidence: Mapped[float | None] = mapped_column(Float)
    producer: Mapped[str] = mapped_column(String(128))
    review_state: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(128))
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    payload_redacted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisProfile(Base):
    __tablename__ = "analysis_profiles"
    __table_args__ = (UniqueConstraint("id", "revision", name="uq_analysis_profile_revision"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    camera_scope_json: Mapped[str | None] = mapped_column(Text)
    time_scope_json: Mapped[str | None] = mapped_column(Text)
    roi_json: Mapped[str | None] = mapped_column(Text)
    object_interests_json: Mapped[str | None] = mapped_column(Text)
    screening_policy_json: Mapped[str | None] = mapped_column(Text)
    candidate_policy_json: Mapped[str | None] = mapped_column(Text)
    semantic_intent: Mapped[str | None] = mapped_column(Text)
    prompt_schema_json: Mapped[str | None] = mapped_column(Text)
    preferred_model_class: Mapped[str | None] = mapped_column(String(128))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(["analysis_profile_id", "analysis_profile_revision"], ["analysis_profiles.id", "analysis_profiles.revision"]),
        Index("ix_analysis_run_asset_state", "asset_id", "state"),
        Index("ix_analysis_run_profile", "analysis_profile_id", "analysis_profile_revision"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("local_assets.id"))
    analysis_profile_id: Mapped[str] = mapped_column(String(128))
    analysis_profile_revision: Mapped[int] = mapped_column(Integer)
    analysis_profile_snapshot_hash: Mapped[str] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(128))
    model_id: Mapped[str | None] = mapped_column(String(255))
    model_version: Mapped[str | None] = mapped_column(String(255))
    runtime_backend: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(64), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class AnalysisEvent(Base):
    __tablename__ = "analysis_events"
    __table_args__ = (Index("ix_analysis_event_asset_offset", "asset_id", "start_offset_ms"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("local_assets.id"))
    start_offset_ms: Mapped[int] = mapped_column(BigInteger)
    end_offset_ms: Mapped[int | None] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_refs_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisArtifact(Base):
    __tablename__ = "analysis_artifacts"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("local_assets.id"))
    artifact_type: Mapped[str] = mapped_column(String(64))
    reference_location: Mapped[str] = mapped_column(Text)
    offset_or_range_json: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
