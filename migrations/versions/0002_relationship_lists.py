"""Add follower/following lists and daily differences.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from app.models import (
    RelationshipChange,
    RelationshipMember,
    RelationshipScan,
    RelationshipScanMember,
)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    RelationshipMember.__table__.create(bind=bind, checkfirst=True)
    RelationshipScan.__table__.create(bind=bind, checkfirst=True)
    RelationshipScanMember.__table__.create(bind=bind, checkfirst=True)
    RelationshipChange.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    RelationshipChange.__table__.drop(bind=bind, checkfirst=True)
    RelationshipScanMember.__table__.drop(bind=bind, checkfirst=True)
    RelationshipScan.__table__.drop(bind=bind, checkfirst=True)
    RelationshipMember.__table__.drop(bind=bind, checkfirst=True)
