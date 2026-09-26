"""Add persistent account priority switch.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("accounts")}
    if "priority_enabled" not in columns:
        op.add_column("accounts", sa.Column("priority_enabled", sa.Boolean(),
                                           nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("accounts", "priority_enabled")
