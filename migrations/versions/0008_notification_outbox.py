"""Add durable Telegram notification outbox.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

from app.models import NotificationOutbox

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    NotificationOutbox.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    NotificationOutbox.__table__.drop(bind=op.get_bind(), checkfirst=True)
