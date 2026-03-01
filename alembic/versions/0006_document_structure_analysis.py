"""Add document structure analysis columns.

- documents.structure_analysis (JSON, nullable)
- extractions.entity_role (VARCHAR(32), nullable)
- extractions.entity_role_confidence (FLOAT, nullable)

Revision ID: 0006_document_structure_analysis
Revises: 0005_projects_and_protocols
Create Date: 2026-03-01

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_document_structure_analysis"
down_revision: str | None = "0005_projects_and_protocols"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- documents: add structure_analysis JSON column --------------------
    op.add_column("documents", sa.Column("structure_analysis", sa.JSON(), nullable=True))

    # -- extractions: add entity role columns -----------------------------
    op.add_column("extractions", sa.Column("entity_role", sa.String(length=32), nullable=True))
    op.add_column("extractions", sa.Column("entity_role_confidence", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("extractions", "entity_role_confidence")
    op.drop_column("extractions", "entity_role")
    op.drop_column("documents", "structure_analysis")
