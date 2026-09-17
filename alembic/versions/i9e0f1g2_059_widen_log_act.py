"""Widen funnel_enrollment_logs.action to varchar(60)

Revision ID: i9e0f1g2_059_widen_log_act
Revises: h8d9e0f1_058_asset_lib
Create Date: 2026-03-02 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'i9e0f1g2_059_widen_log_act'
down_revision = 'h8d9e0f1_058_asset_lib'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        'funnel_enrollment_logs',
        'action',
        type_=sa.String(60),
        existing_type=sa.String(30),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'funnel_enrollment_logs',
        'action',
        type_=sa.String(30),
        existing_type=sa.String(60),
        existing_nullable=False,
    )
