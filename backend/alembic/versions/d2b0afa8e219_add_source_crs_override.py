"""add source_crs_override

Revision ID: d2b0afa8e219
Revises: 0001_initial
Create Date: 2026-05-26

"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "d2b0afa8e219"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "upload_sessions",
        sa.Column("source_crs_override", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("upload_sessions", "source_crs_override")
