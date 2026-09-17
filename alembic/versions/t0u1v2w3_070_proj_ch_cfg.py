"""Add project_channel_configs table and extend chat_widget_configs

Revision ID: t0u1v2w3_070_proj_ch_cfg
Revises: s9t0u1v2_069_ch_registry
Create Date: 2026-03-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 't0u1v2w3_070_proj_ch_cfg'
down_revision = 's9t0u1v2_069_ch_registry'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    existing_tables = insp.get_table_names()

    # ── 1. Create project_channel_configs ─────────────────────────────────
    if "project_channel_configs" not in existing_tables:
        op.create_table(
            "project_channel_configs",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("channel", sa.String(50), nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("config", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        )
        op.create_unique_constraint(
            "uq_proj_ch_cfg_proj_channel",
            "project_channel_configs",
            ["project_id", "channel"],
        )
        op.create_index(
            "ix_proj_ch_cfg_project_id",
            "project_channel_configs",
            ["project_id"],
        )

    # ── 2. Extend chat_widget_configs ─────────────────────────────────────
    widget_cols = {c["name"] for c in insp.get_columns("chat_widget_configs")}

    if "project_id" not in widget_cols:
        op.add_column(
            "chat_widget_configs",
            sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        )

    if "handler_type" not in widget_cols:
        op.add_column(
            "chat_widget_configs",
            sa.Column("handler_type", sa.String(50), nullable=True),
        )

    if "handler_id" not in widget_cols:
        op.add_column(
            "chat_widget_configs",
            sa.Column("handler_id", sa.Integer, nullable=True),
        )

    # ── 3. Backfill project_id from chatbot.project_id ────────────────────
    op.execute("""
        UPDATE chat_widget_configs w
        SET project_id = c.project_id,
            handler_type = 'chatbot',
            handler_id = w.chatbot_id
        FROM chatbots c
        WHERE w.chatbot_id = c.id
          AND w.project_id IS NULL
    """)

    # ── 4. Make project_id NOT NULL (only if column was just added) ───────
    if "project_id" not in widget_cols:
        # Handle any orphaned widgets (no matching chatbot) — assign to first project
        op.execute("""
            UPDATE chat_widget_configs
            SET project_id = (SELECT id FROM projects ORDER BY id LIMIT 1)
            WHERE project_id IS NULL
        """)
        op.alter_column("chat_widget_configs", "project_id", nullable=False)

    # ── 5. Make chatbot_id nullable ───────────────────────────────────────
    op.alter_column("chat_widget_configs", "chatbot_id", nullable=True)

    # Drop the unique constraint on chatbot_id if it exists
    existing_uqs = insp.get_unique_constraints("chat_widget_configs")
    uq_names = {u["name"] for u in existing_uqs if u.get("name")}
    if "chat_widget_configs_chatbot_id_key" in uq_names:
        op.drop_constraint("chat_widget_configs_chatbot_id_key", "chat_widget_configs", type_="unique")

    # ── 6. Add index on project_id for widget configs ─────────────────────
    existing_indexes = {i["name"] for i in insp.get_indexes("chat_widget_configs")}
    if "ix_cwc_project_id" not in existing_indexes:
        op.create_index("ix_cwc_project_id", "chat_widget_configs", ["project_id"])

    # ── 7. Seed project_channel_configs for all existing projects ─────────
    if "project_channel_configs" not in existing_tables:
        op.execute("""
            INSERT INTO project_channel_configs (project_id, channel, enabled)
            SELECT p.id, ch.channel, CASE WHEN ch.channel IN ('whatsapp', 'email', 'sms', 'web') THEN true ELSE false END
            FROM projects p
            CROSS JOIN (
                SELECT unnest(ARRAY['whatsapp', 'email', 'sms', 'web', 'inapp', 'push']) AS channel
            ) ch
            ON CONFLICT (project_id, channel) DO NOTHING
        """)


def downgrade() -> None:
    conn = op.get_bind()
    insp = inspect(conn)
    existing_tables = insp.get_table_names()

    # Restore chatbot_id NOT NULL
    op.execute("""
        UPDATE chat_widget_configs SET chatbot_id = handler_id
        WHERE chatbot_id IS NULL AND handler_type = 'chatbot' AND handler_id IS NOT NULL
    """)

    widget_cols = {c["name"] for c in insp.get_columns("chat_widget_configs")}

    if "handler_id" in widget_cols:
        op.drop_column("chat_widget_configs", "handler_id")
    if "handler_type" in widget_cols:
        op.drop_column("chat_widget_configs", "handler_type")

    existing_indexes = {i["name"] for i in insp.get_indexes("chat_widget_configs")}
    if "ix_cwc_project_id" in existing_indexes:
        op.drop_index("ix_cwc_project_id", "chat_widget_configs")

    if "project_id" in widget_cols:
        op.drop_column("chat_widget_configs", "project_id")

    if "project_channel_configs" in existing_tables:
        op.drop_table("project_channel_configs")
