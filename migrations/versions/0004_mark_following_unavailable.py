"""Mark unavailable following-list scans.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE relationship_scans "
        "SET status = 'unavailable', completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
        "WHERE relationship_type = 'following' AND status != 'complete'"
    )
    op.execute(
        "UPDATE jobs SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP, "
        "error = 'Threads 未公開追蹤中名單' "
        "WHERE kind = 'relationship' AND content_type = 'following' "
        "AND status IN ('queued', 'running')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE relationship_scans SET status = 'failed' "
        "WHERE relationship_type = 'following' AND status = 'unavailable'"
    )
