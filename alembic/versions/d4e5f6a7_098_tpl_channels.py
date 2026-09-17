"""Channel-complete MessagingTemplate (Meta fields, WA instance, external source)

Revision ID: d4e5f6a7_098_tpl_channels
Revises: c3d4e5f6_097_unify_ea
Create Date: 2026-04-13

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'd4e5f6a7_098_tpl_channels'
down_revision = 'c3d4e5f6_097_unify_ea'
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(c['name'] == column for c in insp.get_columns(table))


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    return any(i['name'] == index for i in insp.get_indexes(table))


def upgrade() -> None:
    if not _has_column('messaging_templates', 'meta_template_name'):
        op.add_column('messaging_templates', sa.Column('meta_template_name', sa.String(length=200), nullable=True))
    if not _has_column('messaging_templates', 'meta_language'):
        op.add_column('messaging_templates', sa.Column('meta_language', sa.String(length=20), nullable=True))
    if not _has_column('messaging_templates', 'meta_components'):
        op.add_column('messaging_templates', sa.Column('meta_components', sa.JSON(), nullable=True))
    if not _has_column('messaging_templates', 'whatsapp_instance_id'):
        op.add_column(
            'messaging_templates',
            sa.Column('whatsapp_instance_id', sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            'fk_tpl_whatsapp_instance',
            'messaging_templates',
            'whatsapp_instances',
            ['whatsapp_instance_id'],
            ['id'],
            ondelete='SET NULL',
        )
    if not _has_column('messaging_templates', 'external_source'):
        op.add_column('messaging_templates', sa.Column('external_source', sa.String(length=20), nullable=True))
    if not _has_column('messaging_templates', 'external_last_synced_at'):
        op.add_column('messaging_templates', sa.Column('external_last_synced_at', sa.DateTime(), nullable=True))

    if not _has_index('messaging_templates', 'uq_tpl_meta_import'):
        op.create_index(
            'uq_tpl_meta_import',
            'messaging_templates',
            ['whatsapp_instance_id', 'meta_template_name', 'meta_language'],
            unique=True,
            postgresql_where=sa.text("external_source = 'meta_cloud'"),
        )


def downgrade() -> None:
    if _has_index('messaging_templates', 'uq_tpl_meta_import'):
        op.drop_index('uq_tpl_meta_import', table_name='messaging_templates')
    if _has_column('messaging_templates', 'external_last_synced_at'):
        op.drop_column('messaging_templates', 'external_last_synced_at')
    if _has_column('messaging_templates', 'external_source'):
        op.drop_column('messaging_templates', 'external_source')
    if _has_column('messaging_templates', 'whatsapp_instance_id'):
        op.drop_constraint('fk_tpl_whatsapp_instance', 'messaging_templates', type_='foreignkey')
        op.drop_column('messaging_templates', 'whatsapp_instance_id')
    if _has_column('messaging_templates', 'meta_components'):
        op.drop_column('messaging_templates', 'meta_components')
    if _has_column('messaging_templates', 'meta_language'):
        op.drop_column('messaging_templates', 'meta_language')
    if _has_column('messaging_templates', 'meta_template_name'):
        op.drop_column('messaging_templates', 'meta_template_name')
