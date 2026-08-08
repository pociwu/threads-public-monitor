"""Deduplicate different-resolution copies within content items.

Revision ID: 0006
Revises: 0005
"""

from alembic import op
from sqlalchemy.orm import Session

from app.config import Settings
from app.services.media import deduplicate_content_media_links

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    deduplicate_content_media_links(session, Settings().media_root)
    session.commit()


def downgrade() -> None:
    pass
