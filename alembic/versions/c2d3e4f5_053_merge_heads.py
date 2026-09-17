"""Merge 3 heads into single line

Revision ID: c2d3e4f5_053_merge_heads
Revises: a1b2c3d4_052_arch_align, a3b4c5d6_043_id_resolve, b5c6d7e8_051_media_assets
Create Date: 2026-03-01 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c2d3e4f5_053_merge_heads'
down_revision = ('a1b2c3d4_052_arch_align', 'a3b4c5d6_043_id_resolve', 'b5c6d7e8_051_media_assets')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
