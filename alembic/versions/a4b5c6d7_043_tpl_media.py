"""043 template media url

Revision ID: a4b5c6d7_043_tpl_media
Revises: z3b4c5d6_042_inbound_rtr
Create Date: 2026-02-17 21:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'a4b5c6d7_043_tpl_media'
down_revision = 'z3b4c5d6_042_inbound_rtr'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('messaging_templates', 'media_url'):
        op.add_column(
            'messaging_templates',
            sa.Column('media_url', sa.String(1000), nullable=True)
        )


def downgrade() -> None:
    if column_exists('messaging_templates', 'media_url'):
        op.drop_column('messaging_templates', 'media_url')
