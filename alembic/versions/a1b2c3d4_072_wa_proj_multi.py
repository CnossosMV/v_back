"""Add project_id to whatsapp_instances for multi-instance support

Revision ID: a1b2c3d4_072_wa_proj_multi
Revises: u1v2w3x4_071_email_inb
Create Date: 2026-03-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'a1b2c3d4_072_wa_proj_multi'
down_revision = 'u1v2w3x4_071_email_inb'
branch_labels = None
depends_on = None


def column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = [c['name'] for c in inspector.get_columns(table_name)]
    return column_name in columns


def index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = [idx['name'] for idx in inspector.get_indexes(table_name)]
    return index_name in indexes


def upgrade() -> None:
    # A) Add project_id column to whatsapp_instances (nullable for backfill)
    if not column_exists('whatsapp_instances', 'project_id'):
        op.add_column('whatsapp_instances', sa.Column(
            'project_id', sa.Integer(),
            sa.ForeignKey('projects.id', ondelete='SET NULL'),
            nullable=True,
        ))

    # B) Backfill project_id from handler_channel_links
    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE whatsapp_instances wi
        SET project_id = sub.project_id
        FROM (
            SELECT DISTINCT ON (hcl.instance_id)
                hcl.instance_id,
                COALESCE(c.project_id, at.project_id) AS project_id
            FROM handler_channel_links hcl
            LEFT JOIN chatbots c
                ON hcl.handler_type = 'chatbot' AND hcl.handler_id = c.id
            LEFT JOIN agent_teams at
                ON hcl.handler_type = 'agent_team' AND hcl.handler_id = at.id
            WHERE hcl.channel = 'whatsapp'
              AND hcl.instance_id IS NOT NULL
              AND COALESCE(c.project_id, at.project_id) IS NOT NULL
            ORDER BY hcl.instance_id, hcl.created_at DESC
        ) sub
        WHERE wi.id = sub.instance_id
          AND wi.project_id IS NULL
    """))

    # C) Fallback: assign to the first project in the workspace
    connection.execute(sa.text("""
        UPDATE whatsapp_instances wi
        SET project_id = sub.project_id
        FROM (
            SELECT DISTINCT ON (wi2.id)
                wi2.id AS instance_id,
                p.id AS project_id
            FROM whatsapp_instances wi2
            JOIN projects p ON p.workspace_id = wi2.workspace_id
            WHERE wi2.project_id IS NULL
            ORDER BY wi2.id, p.id
        ) sub
        WHERE wi.id = sub.instance_id
          AND wi.project_id IS NULL
    """))

    # D) Add index on project_id for efficient queries
    if not index_exists('whatsapp_instances', 'ix_wa_inst_project'):
        op.create_index('ix_wa_inst_project', 'whatsapp_instances', ['project_id'])


def downgrade() -> None:
    if index_exists('whatsapp_instances', 'ix_wa_inst_project'):
        op.drop_index('ix_wa_inst_project', table_name='whatsapp_instances')

    if column_exists('whatsapp_instances', 'project_id'):
        op.drop_column('whatsapp_instances', 'project_id')
