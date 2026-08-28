"""Target jobs at stored content for paced refreshes.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    if "content_id" not in columns:
        if bind.dialect.name == "sqlite":
            op.execute(
                "ALTER TABLE jobs ADD COLUMN content_id INTEGER "
                "REFERENCES contents(id) ON DELETE CASCADE"
            )
        else:
            op.add_column("jobs", sa.Column("content_id", sa.Integer(), nullable=True))
            op.create_foreign_key(
                "fk_jobs_content_id_contents",
                "jobs",
                "contents",
                ["content_id"],
                ["id"],
                ondelete="CASCADE",
            )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("jobs")}
    if "ix_jobs_content_id" not in indexes:
        op.create_index("ix_jobs_content_id", "jobs", ["content_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_content_id", table_name="jobs")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(
            "fk_jobs_content_id_contents", "jobs", type_="foreignkey"
        )
    op.drop_column("jobs", "content_id")
