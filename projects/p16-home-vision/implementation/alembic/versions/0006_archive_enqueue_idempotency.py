"""enforce at most one active archive job per source

Revision ID: 0006_archive_enqueue_idempotency
Revises: 0005_normalization_error_code
Create Date: 2026-10-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_archive_enqueue_idempotency"
down_revision: Union[str, None] = "0005_normalization_error_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_STATES = "'queued','resolving_source','downloading','verifying','paused'"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"sqlite", "postgresql"}:
        raise RuntimeError("P16 archive idempotency index requires SQLite or PostgreSQL")

    duplicates = bind.execute(
        sa.text(
            "SELECT source_recording_id FROM archive_jobs "
            f"WHERE state IN ({ACTIVE_STATES}) "
            "GROUP BY source_recording_id HAVING COUNT(*) > 1 LIMIT 10"
        )
    ).scalars().all()
    if duplicates:
        # Never choose or cancel jobs silently during a schema migration. The
        # operator must inspect these IDs and reconcile them through the normal
        # audited P16 control path before retrying this migration.
        raise RuntimeError(
            "P16 active archive-job duplicates require audited reconciliation: "
            + ",".join(str(value) for value in duplicates)
        )

    predicate = f"state IN ({ACTIVE_STATES})"
    op.create_index(
        "uq_archive_jobs_one_active_per_source",
        "archive_jobs",
        ["source_recording_id"],
        unique=True,
        sqlite_where=sa.text(predicate),
        postgresql_where=sa.text(predicate),
    )


def downgrade() -> None:
    op.drop_index("uq_archive_jobs_one_active_per_source", table_name="archive_jobs")
