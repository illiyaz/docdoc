"""Initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-02-08

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Intentionally empty for initial project skeleton.
    pass


def downgrade() -> None:
    pass
