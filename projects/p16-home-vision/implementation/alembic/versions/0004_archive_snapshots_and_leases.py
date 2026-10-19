"""bind archived assets to immutable source snapshots and lease archive jobs"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_archive_snapshots_and_leases"
down_revision: Union[str, None] = "0003_normalization_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "archive_source_snapshots",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("source_recording_id", sa.String(length=128), sa.ForeignKey("source_recordings.id"), nullable=False),
        sa.Column("nvr_id", sa.String(length=128), nullable=False),
        sa.Column("track_id", sa.String(length=64), nullable=False),
        sa.Column("source_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("locator_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("source_size_if_known", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_archive_source_snapshots_source_recording_id", "archive_source_snapshots", ["source_recording_id"])
    with op.batch_alter_table("local_assets") as batch:
        batch.add_column(sa.Column("archive_source_snapshot_id", sa.String(length=128), nullable=True))
        batch.create_foreign_key("fk_local_assets_archive_source_snapshot", "archive_source_snapshots", ["archive_source_snapshot_id"], ["id"])
        batch.create_index("ix_local_assets_archive_source_snapshot_id", ["archive_source_snapshot_id"])
    for table, name in (("archive_jobs", "worker_id"), ("archive_jobs", "lease_token"), ("archive_jobs", "lease_expires_at"), ("archive_jobs", "lease_phase")):
        column = sa.String(length=128) if name == "worker_id" else sa.String(length=64) if name == "lease_token" else sa.DateTime(timezone=True) if name == "lease_expires_at" else sa.String(length=32)
        op.add_column(table, sa.Column(name, column, nullable=True))
    op.create_index("ix_archive_jobs_worker_id", "archive_jobs", ["worker_id"])
    op.create_index("ix_archive_jobs_lease_expires_at", "archive_jobs", ["lease_expires_at"])

    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT la.id, sr.id, sr.nvr_id, sr.track_id, sr.source_start, sr.source_end,
               sr.duration_ms, sr.locator_fingerprint, sr.source_size_if_known
        FROM local_assets la JOIN source_recordings sr ON sr.id = la.source_recording_id
        WHERE la.asset_role = 'source_archive' AND la.archive_source_snapshot_id IS NULL
    """)).fetchall()
    for row in rows:
        snapshot_id = f"snapshot-{row[0]}"
        bind.execute(sa.text("""
            INSERT INTO archive_source_snapshots
            (id, source_recording_id, nvr_id, track_id, source_start, source_end, duration_ms,
             locator_fingerprint, source_size_if_known, created_at)
            VALUES (:id, :source_id, :nvr_id, :track_id, :source_start, :source_end, :duration_ms,
                    :locator, :size, CURRENT_TIMESTAMP)
        """), {"id": snapshot_id, "source_id": row[1], "nvr_id": row[2], "track_id": row[3],
               "source_start": row[4], "source_end": row[5], "duration_ms": row[6],
               "locator": row[7], "size": row[8]})
        bind.execute(sa.text("UPDATE local_assets SET archive_source_snapshot_id = :snapshot_id WHERE id = :asset_id"), {"snapshot_id": snapshot_id, "asset_id": row[0]})


def downgrade() -> None:
    op.drop_index("ix_archive_jobs_lease_expires_at", table_name="archive_jobs")
    op.drop_index("ix_archive_jobs_worker_id", table_name="archive_jobs")
    for name in ("lease_phase", "lease_expires_at", "lease_token", "worker_id"):
        op.drop_column("archive_jobs", name)
    with op.batch_alter_table("local_assets") as batch:
        batch.drop_index("ix_local_assets_archive_source_snapshot_id")
        batch.drop_constraint("fk_local_assets_archive_source_snapshot", type_="foreignkey")
        batch.drop_column("archive_source_snapshot_id")
    op.drop_index("ix_archive_source_snapshots_source_recording_id", table_name="archive_source_snapshots")
    op.drop_table("archive_source_snapshots")
