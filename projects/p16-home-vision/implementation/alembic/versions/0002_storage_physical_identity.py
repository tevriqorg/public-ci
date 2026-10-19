"""record physical storage identity when it is known"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_storage_physical_identity"
down_revision: Union[str, None] = "0001_manifest_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("storage_volumes", sa.Column("physical_device_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("storage_volumes", "physical_device_id")
