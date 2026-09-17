"""Add reply_to to messaging_templates

Revision ID: c3d4e5f6_074_tpl_reply_to
Revises: b2c3d4e5_073_nocode_ext
Create Date: 2026-03-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'c3d4e5f6_074_tpl_reply_to'
down_revision = 'b2c3d4e5_073_nocode_ext'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('messaging_templates', 'reply_to'):
        op.add_column(
            'messaging_templates',
            sa.Column('reply_to', sa.String(255), nullable=True)
        )


def downgrade() -> None:
    if column_exists('messaging_templates', 'reply_to'):
        op.drop_column('messaging_templates', 'reply_to')
