"""Add feedback_webhook_secret to customer_smtp_configs

Revision ID: s0t1u2v3_089_smtp_fdbk
Revises: r9s0t1u2_088_click_trk
Create Date: 2026-03-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 's0t1u2v3_089_smtp_fdbk'
down_revision = 'r9s0t1u2_088_click_trk'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not column_exists('customer_smtp_configs', 'feedback_webhook_secret'):
        op.add_column(
            'customer_smtp_configs',
            sa.Column('feedback_webhook_secret', sa.String(64), nullable=True),
        )


def downgrade() -> None:
    if column_exists('customer_smtp_configs', 'feedback_webhook_secret'):
        op.drop_column('customer_smtp_configs', 'feedback_webhook_secret')
