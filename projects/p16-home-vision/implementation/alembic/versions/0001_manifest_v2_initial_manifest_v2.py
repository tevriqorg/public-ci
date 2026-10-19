"""initial manifest v2

Revision ID: 0001_manifest_v2
Revises:
Create Date: 2026-08-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_manifest_v2"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analysis_profiles",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("camera_scope_json", sa.Text(), nullable=True),
        sa.Column("time_scope_json", sa.Text(), nullable=True),
        sa.Column("roi_json", sa.Text(), nullable=True),
        sa.Column("object_interests_json", sa.Text(), nullable=True),
        sa.Column("screening_policy_json", sa.Text(), nullable=True),
        sa.Column("candidate_policy_json", sa.Text(), nullable=True),
        sa.Column("semantic_intent", sa.Text(), nullable=True),
        sa.Column("prompt_schema_json", sa.Text(), nullable=True),
        sa.Column("preferred_model_class", sa.String(length=128), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", "revision"),
        sa.UniqueConstraint("id", "revision", name="uq_analysis_profile_revision"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=128), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("payload_redacted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_events_entity_id"), "audit_events", ["entity_id"], unique=False)
    op.create_index(op.f("ix_audit_events_event_type"), "audit_events", ["event_type"], unique=False)
    op.create_table(
        "nvr_devices",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("firmware", sa.String(length=255), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "storage_volumes",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("mount_path_runtime", sa.Text(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("free_bytes", sa.BigInteger(), nullable=True),
        sa.Column("online", sa.Boolean(), nullable=False),
        sa.Column("writable", sa.Boolean(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "camera_tracks",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("nvr_id", sa.String(length=128), nullable=False),
        sa.Column("track_id", sa.String(length=64), nullable=False),
        sa.Column("input_channel", sa.String(length=64), nullable=True),
        sa.Column("stream_role", sa.String(length=32), nullable=False),
        sa.Column("camera_name", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["nvr_id"], ["nvr_devices.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nvr_id", "track_id", name="uq_camera_track_identity"),
    )
    op.create_index(op.f("ix_camera_tracks_nvr_id"), "camera_tracks", ["nvr_id"], unique=False)
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("nvr_id", sa.String(length=128), nullable=False),
        sa.Column("track_id", sa.String(length=64), nullable=False),
        sa.Column("query_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("query_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("response_hash", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('running','success','partial','failed')", name="ck_scan_state"),
        sa.ForeignKeyConstraint(["nvr_id", "track_id"], ["camera_tracks.nvr_id", "camera_tracks.track_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_range", "scan_runs", ["nvr_id", "track_id", "query_start", "query_end"], unique=False)
    op.create_table(
        "source_recordings",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("nvr_id", sa.String(length=128), nullable=False),
        sa.Column("track_id", sa.String(length=64), nullable=False),
        sa.Column("source_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("locator_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("source_size_if_known", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("recording_type", sa.String(length=128), nullable=True),
        sa.Column("source_state", sa.String(length=32), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_scan_id", sa.String(length=128), nullable=True),
        sa.Column("last_scan_id", sa.String(length=128), nullable=True),
        sa.Column("raw_metadata_hash", sa.String(length=128), nullable=True),
        sa.CheckConstraint("source_state IN ('available','expired','unknown')", name="ck_source_state"),
        sa.ForeignKeyConstraint(["first_scan_id"], ["scan_runs.id"]),
        sa.ForeignKeyConstraint(["last_scan_id"], ["scan_runs.id"]),
        sa.ForeignKeyConstraint(["nvr_id", "track_id"], ["camera_tracks.nvr_id", "camera_tracks.track_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nvr_id", "track_id", "source_start", "source_end", name="uq_source_identity"),
    )
    op.create_index("ix_source_state_start", "source_recordings", ["source_state", "source_start"], unique=False)
    op.create_index("ix_source_track_start", "source_recordings", ["nvr_id", "track_id", "source_start"], unique=False)
    op.create_table(
        "archive_jobs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("source_recording_id", sa.String(length=128), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("bytes_received", sa.BigInteger(), nullable=False),
        sa.Column("temp_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('queued','resolving_source','downloading','verifying','completed','failed','cancelled','paused')", name="ck_archive_job_state"),
        sa.ForeignKeyConstraint(["source_recording_id"], ["source_recordings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_archive_job_state_requested", "archive_jobs", ["state", "requested_at"], unique=False)
    op.create_index(op.f("ix_archive_jobs_source_recording_id"), "archive_jobs", ["source_recording_id"], unique=False)
    op.create_table(
        "local_assets",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("source_recording_id", sa.String(length=128), nullable=False),
        sa.Column("asset_role", sa.String(length=64), nullable=False),
        sa.Column("storage_volume_id", sa.String(length=128), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("container", sa.String(length=64), nullable=True),
        sa.Column("video_codec", sa.String(length=64), nullable=True),
        sa.Column("audio_codec", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("media_duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("verification_state", sa.String(length=32), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("lifecycle_state IN ('present','missing','deleting','deleted')", name="ck_asset_lifecycle_state"),
        sa.CheckConstraint("verification_state IN ('pending','verified','failed')", name="ck_asset_verification_state"),
        sa.ForeignKeyConstraint(["source_recording_id"], ["source_recordings.id"]),
        sa.ForeignKeyConstraint(["storage_volume_id"], ["storage_volumes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_lifecycle", "local_assets", ["lifecycle_state"], unique=False)
    op.create_index("ix_asset_source", "local_assets", ["source_recording_id"], unique=False)
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_profile_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_profile_revision", sa.Integer(), nullable=False),
        sa.Column("analysis_profile_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("pipeline_version", sa.String(length=128), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=True),
        sa.Column("model_version", sa.String(length=255), nullable=True),
        sa.Column("runtime_backend", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["analysis_profile_id", "analysis_profile_revision"], ["analysis_profiles.id", "analysis_profiles.revision"]),
        sa.ForeignKeyConstraint(["asset_id"], ["local_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analysis_run_asset_state", "analysis_runs", ["asset_id", "state"], unique=False)
    op.create_index("ix_analysis_run_profile", "analysis_runs", ["analysis_profile_id", "analysis_profile_revision"], unique=False)
    op.create_table(
        "delete_intents",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("local_asset_id", sa.String(length=128), nullable=False),
        sa.Column("source_recording_id", sa.String(length=128), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("expected_relative_path", sa.Text(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("expected_size", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.CheckConstraint("state IN ('previewed','confirmed','deleting','completed','failed','cancelled')", name="ck_delete_intent_state"),
        sa.ForeignKeyConstraint(["local_asset_id"], ["local_assets.id"]),
        sa.ForeignKeyConstraint(["source_recording_id"], ["source_recordings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "review_flags",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("flag_type", sa.String(length=128), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("producer", sa.String(length=128), nullable=False),
        sa.Column("review_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("review_state IN ('pending','accepted','rejected')", name="ck_review_state"),
        sa.ForeignKeyConstraint(["asset_id"], ["local_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_state_type", "review_flags", ["review_state", "flag_type"], unique=False)
    op.create_table(
        "tombstones",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("source_recording_id", sa.String(length=128), nullable=False),
        sa.Column("local_asset_id", sa.String(length=128), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("previous_relative_path", sa.Text(), nullable=True),
        sa.Column("previous_size", sa.BigInteger(), nullable=True),
        sa.Column("previous_sha256", sa.String(length=64), nullable=True),
        sa.Column("suppress_redownload", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["local_asset_id"], ["local_assets.id"]),
        sa.ForeignKeyConstraint(["source_recording_id"], ["source_recordings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tombstones_source_recording_id"), "tombstones", ["source_recording_id"], unique=False)
    op.create_table(
        "analysis_artifacts",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("reference_location", sa.Text(), nullable=False),
        sa.Column("offset_or_range_json", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["local_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_analysis_artifacts_analysis_run_id"), "analysis_artifacts", ["analysis_run_id"], unique=False)
    op.create_table(
        "analysis_events",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("start_offset_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_offset_ms", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence_refs_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["local_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analysis_event_asset_offset", "analysis_events", ["asset_id", "start_offset_ms"], unique=False)
    op.create_index(op.f("ix_analysis_events_analysis_run_id"), "analysis_events", ["analysis_run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_analysis_events_analysis_run_id"), table_name="analysis_events")
    op.drop_index("ix_analysis_event_asset_offset", table_name="analysis_events")
    op.drop_table("analysis_events")
    op.drop_index(op.f("ix_analysis_artifacts_analysis_run_id"), table_name="analysis_artifacts")
    op.drop_table("analysis_artifacts")
    op.drop_index(op.f("ix_tombstones_source_recording_id"), table_name="tombstones")
    op.drop_table("tombstones")
    op.drop_index("ix_review_state_type", table_name="review_flags")
    op.drop_table("review_flags")
    op.drop_table("delete_intents")
    op.drop_index("ix_analysis_run_profile", table_name="analysis_runs")
    op.drop_index("ix_analysis_run_asset_state", table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_index("ix_asset_source", table_name="local_assets")
    op.drop_index("ix_asset_lifecycle", table_name="local_assets")
    op.drop_table("local_assets")
    op.drop_index(op.f("ix_archive_jobs_source_recording_id"), table_name="archive_jobs")
    op.drop_index("ix_archive_job_state_requested", table_name="archive_jobs")
    op.drop_table("archive_jobs")
    op.drop_index("ix_source_track_start", table_name="source_recordings")
    op.drop_index("ix_source_state_start", table_name="source_recordings")
    op.drop_table("source_recordings")
    op.drop_index("ix_scan_range", table_name="scan_runs")
    op.drop_table("scan_runs")
    op.drop_index(op.f("ix_camera_tracks_nvr_id"), table_name="camera_tracks")
    op.drop_table("camera_tracks")
    op.drop_table("storage_volumes")
    op.drop_table("nvr_devices")
    op.drop_index(op.f("ix_audit_events_event_type"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_entity_id"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("analysis_profiles")
