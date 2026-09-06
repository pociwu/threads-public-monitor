"""Confirm relationship removals with a second traversal.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("relationship_scans")
    }
    if "removal_confirmation_fingerprint" not in columns:
        op.add_column(
            "relationship_scans",
            sa.Column(
                "removal_confirmation_fingerprint",
                sa.String(length=64),
                nullable=True,
            ),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("relationship_scans")
    }
    if "removal_confirmation_fingerprint" in columns:
        op.drop_column("relationship_scans", "removal_confirmation_fingerprint")
