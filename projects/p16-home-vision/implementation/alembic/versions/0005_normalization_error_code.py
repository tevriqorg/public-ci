"""retain a safe normalization failure code"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_normalization_error_code"
down_revision: Union[str, None] = "0004_archive_snapshots_and_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("local_assets", sa.Column("normalization_error_code", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("local_assets", "normalization_error_code")
