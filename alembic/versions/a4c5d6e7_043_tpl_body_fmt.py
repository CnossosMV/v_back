"""Add body_format to messaging_templates

Revision ID: a4c5d6e7_043_tpl_body_fmt
Revises: z3b4c5d6_042_inbound_rtr
Create Date: 2026-03-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'a4c5d6e7_043_tpl_body_fmt'
down_revision = 'c3d4e5f6_074_tpl_reply_to'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('messaging_templates', 'body_format'):
        op.add_column(
            'messaging_templates',
            sa.Column('body_format', sa.String(10), nullable=True, server_default='html'),
        )


def downgrade() -> None:
    if column_exists('messaging_templates', 'body_format'):
        op.drop_column('messaging_templates', 'body_format')
