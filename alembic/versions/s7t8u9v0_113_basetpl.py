"""Add template fields to base_cell_messages (WhatsApp templates)

Revision ID: s7t8u9v0_113_basetpl
Revises: r6s7t8u9_112_basecell
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 's7t8u9v0_113_basetpl'
down_revision = 'r6s7t8u9_112_basecell'
branch_labels = None
depends_on = None


def _columns(table: str):
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    cols = _columns("base_cell_messages")
    if "content_mode" not in cols:
        op.add_column("base_cell_messages",
                      sa.Column("content_mode", sa.String(length=20), nullable=False, server_default="text"))
    if "template_name" not in cols:
        op.add_column("base_cell_messages", sa.Column("template_name", sa.String(length=255), nullable=True))
    if "template_language" not in cols:
        op.add_column("base_cell_messages", sa.Column("template_language", sa.String(length=20), nullable=True))
    if "instance_id" not in cols:
        op.add_column("base_cell_messages", sa.Column("instance_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    cols = _columns("base_cell_messages")
    for name in ("instance_id", "template_language", "template_name", "content_mode"):
        if name in cols:
            op.drop_column("base_cell_messages", name)
