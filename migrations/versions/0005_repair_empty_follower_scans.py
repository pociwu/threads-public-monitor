"""Repair completed empty follower scans for non-empty profiles.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    RelationshipChange,
    RelationshipMember,
    RelationshipScan,
    RelationshipScanMember,
)

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    suspicious = session.scalars(
        select(RelationshipScan)
        .join(Account, Account.id == RelationshipScan.account_id)
        .where(
            RelationshipScan.relationship_type == "followers",
            RelationshipScan.status == "complete",
            RelationshipScan.collected_count == 0,
            Account.follower_count > 0,
        )
    ).all()
    for scan in suspicious:
        session.execute(
            delete(RelationshipChange).where(RelationshipChange.scan_id == scan.id)
        )
        scan.status = "failed"

        previous_scan_id = session.scalar(
            select(RelationshipScanMember.scan_id)
            .join(RelationshipScan, RelationshipScan.id == RelationshipScanMember.scan_id)
            .where(
                RelationshipScan.account_id == scan.account_id,
                RelationshipScan.relationship_type == "followers",
                RelationshipScan.id != scan.id,
            )
            .order_by(RelationshipScan.scan_date.desc(), RelationshipScan.id.desc())
            .limit(1)
        )
        if previous_scan_id is None:
            continue
        member_ids = session.scalars(
            select(RelationshipScanMember.member_id).where(
                RelationshipScanMember.scan_id == previous_scan_id
            )
        ).all()
        members = session.scalars(
            select(RelationshipMember).where(RelationshipMember.id.in_(member_ids))
        ).all()
        for member in members:
            member.active = True
            member.removed_at = None
    session.commit()


def downgrade() -> None:
    pass
