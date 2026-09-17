"""MES v2: reply signal, settle lifecycle, worker state

Revision ID: 122_mes_v2
Revises: 121_funnel_score
Create Date: 2026-07-04 00:10:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = '122_mes_v2'
down_revision = '121_funnel_score'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return any(c["name"] == column_name for c in inspector.get_columns(table_name))


def index_exists(index_name: str, table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    # send_logs: inbound reply attribution
    if not column_exists("send_logs", "replied_at"):
        op.add_column("send_logs", sa.Column("replied_at", sa.DateTime(), nullable=True))
    if not column_exists("send_logs", "reply_count"):
        op.add_column(
            "send_logs",
            sa.Column("reply_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )

    # message_effectiveness_scores: settle lifecycle
    if not column_exists("message_effectiveness_scores", "settled"):
        op.add_column(
            "message_effectiveness_scores",
            sa.Column("settled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if not index_exists("ix_mes_unsettled", "message_effectiveness_scores"):
        op.create_index(
            "ix_mes_unsettled",
            "message_effectiveness_scores",
            ["project_id", "settled"],
            postgresql_where=sa.text("settled = false"),
        )

    # mes_config: per-channel link tracking opt-in (email is always tracked)
    if not column_exists("mes_config", "link_tracking_channels"):
        op.add_column("mes_config", sa.Column("link_tracking_channels", JSONB(), nullable=True))

    # worker_state: persistent watermarks / cursors for background workers
    if not table_exists("worker_state"):
        op.create_table(
            "worker_state",
            sa.Column("key", sa.String(length=100), primary_key=True),
            sa.Column("value", JSONB(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    if table_exists("worker_state"):
        op.drop_table("worker_state")
    if column_exists("mes_config", "link_tracking_channels"):
        op.drop_column("mes_config", "link_tracking_channels")
    if index_exists("ix_mes_unsettled", "message_effectiveness_scores"):
        op.drop_index("ix_mes_unsettled", table_name="message_effectiveness_scores")
    if column_exists("message_effectiveness_scores", "settled"):
        op.drop_column("message_effectiveness_scores", "settled")
    if column_exists("send_logs", "reply_count"):
        op.drop_column("send_logs", "reply_count")
    if column_exists("send_logs", "replied_at"):
        op.drop_column("send_logs", "replied_at")
