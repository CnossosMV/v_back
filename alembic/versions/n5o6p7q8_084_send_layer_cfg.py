"""Add use_send_layer to project_send_configs

Revision ID: n5o6p7q8_084_sl_cfg
Revises: m4n5o6p7_083_blk_cnt
Create Date: 2026-03-25 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'n5o6p7q8_084_sl_cfg'
down_revision = 'm4n5o6p7_083_blk_cnt'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    insp = inspect(connection)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('project_send_configs', 'use_send_layer'):
        op.add_column(
            'project_send_configs',
            sa.Column('use_send_layer', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        )


def downgrade() -> None:
    if column_exists('project_send_configs', 'use_send_layer'):
        op.drop_column('project_send_configs', 'use_send_layer')
