"""coordinate normalization workers with expiring leases"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_normalization_leases"
down_revision: Union[str, None] = "0002_storage_physical_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("local_assets", sa.Column("normalization_worker_id", sa.String(length=128), nullable=True))
    op.add_column("local_assets", sa.Column("normalization_lease_token", sa.String(length=64), nullable=True))
    op.add_column("local_assets", sa.Column("normalization_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("local_assets", sa.Column("normalization_phase", sa.String(length=32), nullable=True))
    op.add_column("local_assets", sa.Column("normalization_attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.create_index("ix_local_assets_normalization_worker_id", "local_assets", ["normalization_worker_id"])
    op.create_index("ix_local_assets_normalization_lease_expires_at", "local_assets", ["normalization_lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_local_assets_normalization_lease_expires_at", table_name="local_assets")
    op.drop_index("ix_local_assets_normalization_worker_id", table_name="local_assets")
    op.drop_column("local_assets", "normalization_attempt_count")
    op.drop_column("local_assets", "normalization_phase")
    op.drop_column("local_assets", "normalization_lease_expires_at")
    op.drop_column("local_assets", "normalization_lease_token")
    op.drop_column("local_assets", "normalization_worker_id")
