"""Backfill canonical quiet hours policy

Revision ID: 143_quiet_hours_policy
Revises: 142_cap_provider
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa


revision = "143_quiet_hours_policy"
down_revision = "142_cap_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Copy legacy quiet hours only where the canonical value is empty."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if not {"project_send_configs", "project_policies"}.issubset(tables):
        return

    op.execute(sa.text("""
        INSERT INTO project_policies (
            project_id, quiet_hours, is_active, created_at, updated_at
        )
        SELECT
            cfg.project_id,
            jsonb_build_object(
                'enabled', true,
                'start', cfg.quiet_hours_start,
                'end', cfg.quiet_hours_end,
                'timezone', COALESCE(cfg.quiet_hours_timezone, 'UTC'),
                'channels', jsonb_build_array()
            ),
            true,
            now(),
            now()
        FROM project_send_configs AS cfg
        WHERE cfg.quiet_hours_enabled = true
          AND cfg.quiet_hours_start IS NOT NULL
          AND cfg.quiet_hours_end IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM project_policies AS policy
              WHERE policy.project_id = cfg.project_id
          )
        ON CONFLICT (project_id) DO NOTHING
    """))
    op.execute(sa.text("""
        UPDATE project_policies AS policy
        SET quiet_hours = jsonb_build_object(
                'enabled', true,
                'start', cfg.quiet_hours_start,
                'end', cfg.quiet_hours_end,
                'timezone', COALESCE(cfg.quiet_hours_timezone, 'UTC'),
                'channels', jsonb_build_array()
            ),
            updated_at = now()
        FROM project_send_configs AS cfg
        WHERE policy.project_id = cfg.project_id
          AND policy.quiet_hours IS NULL
          AND cfg.quiet_hours_enabled = true
          AND cfg.quiet_hours_start IS NOT NULL
          AND cfg.quiet_hours_end IS NOT NULL
    """))


def downgrade() -> None:
    # The copied value may have been edited after rollout.  Never erase it.
    pass
