"""Add undone_at to contact_merge_logs

Revision ID: x5y6z7a8_094_undo_merge
Revises: w4x5y6z7_093_retry_col
Create Date: 2026-03-31

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'x5y6z7a8_094_undo_merge'
down_revision = 'w4x5y6z7_093_retry_col'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('contact_merge_logs', 'undone_at'):
        op.add_column('contact_merge_logs',
            sa.Column('undone_at', sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    if column_exists('contact_merge_logs', 'undone_at'):
        op.drop_column('contact_merge_logs', 'undone_at')
