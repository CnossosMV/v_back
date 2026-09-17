"""Backfill user_id on send_logs from messaging_users

Revision ID: q8r9s0t1_087_bkfl_uid
Revises: p7q8r9s0_086_dlvry_fdbk
Create Date: 2026-03-26 20:30:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'q8r9s0t1_087_bkfl_uid'
down_revision = 'p7q8r9s0_086_dlvry_fdbk'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE send_logs sl
        SET user_id = mu.id
        FROM messaging_users mu
        WHERE sl.user_id IS NULL
          AND mu.project_id = sl.project_id
          AND (mu.email = sl.recipient OR mu.phone = sl.recipient OR mu.phone_e164 = sl.recipient)
    """)


def downgrade() -> None:
    pass
