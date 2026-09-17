"""Align legacy customer and inbox model columns.

Revision ID: 131_schema_align
Revises: 130_wa_meta_proj
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "131_schema_align"
down_revision = "130_wa_meta_proj"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(
        column["name"] == column_name
        for column in inspect(op.get_bind()).get_columns(table_name)
    )


def _index_exists(table_name: str, index_name: str) -> bool:
    return any(
        index["name"] == index_name
        for index in inspect(op.get_bind()).get_indexes(table_name)
    )


def upgrade() -> None:
    if not _table_exists("customers"):
        op.create_table(
            "customers",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("phone", sa.String(length=20), nullable=True),
            sa.Column("address", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_customers_id", "customers", ["id"], unique=False)
        op.create_index("ix_customers_email", "customers", ["email"], unique=True)

    if _table_exists("support_tickets") and not _column_exists("support_tickets", "customer_phone"):
        op.add_column(
            "support_tickets",
            sa.Column("customer_phone", sa.String(length=50), nullable=True),
        )

    if _table_exists("messaging_templates"):
        has_old = _column_exists("messaging_templates", "metadata")
        has_new = _column_exists("messaging_templates", "template_metadata")
        if has_old and not has_new:
            op.alter_column(
                "messaging_templates",
                "metadata",
                new_column_name="template_metadata",
            )
        elif has_old and has_new:
            op.execute(
                sa.text(
                    "UPDATE messaging_templates "
                    "SET template_metadata = COALESCE(template_metadata, metadata)"
                )
            )
            op.drop_column("messaging_templates", "metadata")


def downgrade() -> None:
    if _table_exists("messaging_templates"):
        has_old = _column_exists("messaging_templates", "metadata")
        has_new = _column_exists("messaging_templates", "template_metadata")
        if has_new and not has_old:
            op.alter_column(
                "messaging_templates",
                "template_metadata",
                new_column_name="metadata",
            )

    if _table_exists("support_tickets") and _column_exists("support_tickets", "customer_phone"):
        op.drop_column("support_tickets", "customer_phone")

    if _table_exists("customers"):
        if _index_exists("customers", "ix_customers_email"):
            op.drop_index("ix_customers_email", table_name="customers")
        if _index_exists("customers", "ix_customers_id"):
            op.drop_index("ix_customers_id", table_name="customers")
        op.drop_table("customers")
