"""Fix decision_trace: wrap dict values in list

Revision ID: o6p7q8r9_085_fix_dt
Revises: n5o6p7q8_084_sl_cfg
Create Date: 2026-03-25 16:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'o6p7q8r9_085_fix_dt'
down_revision = 'n5o6p7q8_084_sl_cfg'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE send_logs
        SET decision_trace = jsonb_build_array(decision_trace::jsonb)
        WHERE decision_trace IS NOT NULL
          AND jsonb_typeof(decision_trace::jsonb) = 'object'
    """)


def downgrade() -> None:
    pass
