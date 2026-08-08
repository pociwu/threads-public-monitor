"""Deduplicate media links within each content item.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
from sqlalchemy.orm import Session

from app.services.media import deduplicate_content_media_links

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    deduplicate_content_media_links(session)


def downgrade() -> None:
    pass
