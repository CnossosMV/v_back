"""Add FunnelScore Meta mapping

Revision ID: 121_funnel_score
Revises: 120_dedupe_key_len
Create Date: 2026-06-22

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "121_funnel_score"
down_revision = "120_dedupe_key_len"
branch_labels = None
depends_on = None


def _has_tables(*names: str) -> bool:
    existing = set(inspect(op.get_bind()).get_table_names())
    return all(name in existing for name in names)


def upgrade() -> None:
    if not _has_tables("messaging_destinations", "messaging_event_schemas", "messaging_event_mappings"):
        return

    op.execute(sa.text("""
        INSERT INTO messaging_event_schemas (
            project_id,
            event_name,
            display_name,
            description,
            category,
            properties_schema,
            required_properties,
            is_standard,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            meta_projects.project_id,
            'funnel.score',
            'FunnelScore',
            'Meta-only redundant funnel value signal.',
            'experiment',
            '{"type":"object","properties":{"funnel_step":{"type":"string"},"score_value":{"type":"number"},"source_event_name":{"type":"string"},"source_event_id":{"type":"string"},"value":{"type":"number"},"currency":{"type":"string"}}}'::json,
            ARRAY['funnel_step', 'score_value', 'source_event_name', 'source_event_id'],
            false,
            true,
            now(),
            now()
        FROM (
            SELECT d.project_id
            FROM messaging_destinations d
            WHERE d.destination_type = 'meta_pixel'
              AND d.is_active = true
            GROUP BY d.project_id
        ) meta_projects
        WHERE NOT EXISTS (
              SELECT 1
              FROM messaging_event_schemas s
              WHERE s.project_id = meta_projects.project_id
                AND s.event_name = 'funnel.score'
          )
    """))

    op.execute(sa.text("""
        INSERT INTO messaging_event_mappings (
            project_id,
            event_schema_id,
            destination_id,
            destination_event_name,
            property_mappings,
            provider_settings,
            is_active,
            created_at,
            updated_at
        )
        SELECT
            d.project_id,
            s.id,
            d.id,
            'FunnelScore',
            NULL,
            NULL,
            true,
            now(),
            now()
        FROM messaging_destinations d
        JOIN messaging_event_schemas s
          ON s.project_id = d.project_id
         AND s.event_name = 'funnel.score'
        WHERE d.destination_type = 'meta_pixel'
          AND d.is_active = true
          AND NOT EXISTS (
              SELECT 1
              FROM messaging_event_mappings m
              WHERE m.event_schema_id = s.id
                AND m.destination_id = d.id
          )
    """))


def downgrade() -> None:
    if not _has_tables("messaging_event_schemas", "messaging_event_mappings"):
        return

    op.execute(sa.text("""
        DELETE FROM messaging_event_mappings m
        USING messaging_event_schemas s
        WHERE m.event_schema_id = s.id
          AND s.event_name = 'funnel.score'
          AND m.destination_event_name = 'FunnelScore'
    """))
    op.execute(sa.text("""
        DELETE FROM messaging_event_schemas
        WHERE event_name = 'funnel.score'
    """))
