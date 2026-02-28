"""Add projects, protocol_configs, density_summaries, export_jobs, llm_call_logs.

Extend ingestion_runs, documents, notification_subjects, notification_lists
with project_id FK and cataloging fields.

Revision ID: 0005_projects_and_protocols
Revises: 0004_add_audit_and_review
Create Date: 2026-02-28

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_projects_and_protocols"
down_revision: str | None = "0004_add_audit_and_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- projects --------------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # -- protocol_configs ------------------------------------------------------
    op.create_table(
        "protocol_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("base_protocol_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )

    # -- density_summaries -----------------------------------------------------
    op.create_table(
        "density_summaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("total_entities", sa.Integer(), nullable=False),
        sa.Column("by_category", sa.JSON(), nullable=True),
        sa.Column("by_type", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("confidence_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
    )

    # -- export_jobs -----------------------------------------------------------
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("protocol_config_id", sa.Uuid(), nullable=True),
        sa.Column("export_type", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(length=2048), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("filters_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["protocol_config_id"], ["protocol_configs.id"], ondelete="SET NULL",
        ),
    )

    # -- llm_call_logs ---------------------------------------------------------
    op.create_table(
        "llm_call_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("use_case", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=128), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
    )

    # -- Extend existing tables ------------------------------------------------

    # ingestion_runs: add project_id FK
    op.add_column(
        "ingestion_runs",
        sa.Column("project_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ingestion_runs_project_id",
        "ingestion_runs",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # documents: add structure_class, can_auto_process, manual_review_reason
    op.add_column(
        "documents",
        sa.Column("structure_class", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "can_auto_process",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "documents",
        sa.Column("manual_review_reason", sa.String(length=256), nullable=True),
    )

    # notification_subjects: add project_id FK
    op.add_column(
        "notification_subjects",
        sa.Column("project_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_subjects_project_id",
        "notification_subjects",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # notification_lists: add project_id FK
    op.add_column(
        "notification_lists",
        sa.Column("project_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_lists_project_id",
        "notification_lists",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Drop FKs on existing tables
    op.drop_constraint("fk_notification_lists_project_id", "notification_lists", type_="foreignkey")
    op.drop_column("notification_lists", "project_id")

    op.drop_constraint("fk_notification_subjects_project_id", "notification_subjects", type_="foreignkey")
    op.drop_column("notification_subjects", "project_id")

    op.drop_column("documents", "manual_review_reason")
    op.drop_column("documents", "can_auto_process")
    op.drop_column("documents", "structure_class")

    op.drop_constraint("fk_ingestion_runs_project_id", "ingestion_runs", type_="foreignkey")
    op.drop_column("ingestion_runs", "project_id")

    # Drop new tables
    op.drop_table("llm_call_logs")
    op.drop_table("export_jobs")
    op.drop_table("density_summaries")
    op.drop_table("protocol_configs")
    op.drop_table("projects")
