"""Deduplicate Meta CDN resolution variants and retain the largest asset.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
from sqlalchemy.orm import Session

from app.services.media import deduplicate_canonical_content_media_links

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    deduplicate_canonical_content_media_links(session)
    session.commit()


def downgrade() -> None:
    pass
