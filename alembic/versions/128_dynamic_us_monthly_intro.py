"""Use live pricing variables in US Pro Monthly Intro subjects.

Revision ID: 128_dynamic_us_monthly_intro
Revises: 127_meta_channels
Create Date: 2026-07-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "128_dynamic_us_monthly_intro"
down_revision = "127_meta_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE messaging_templates
               SET subject = CASE locale
                   WHEN 'en-US' THEN 'Pro Monthly: {{promotional_price_label}} for your first {{promotional_cycles}} billing cycles'
                   WHEN 'es-US' THEN 'Pro Mensual: {{promotional_price_label}} durante tus primeros {{promotional_cycles}} ciclos'
               END,
                   updated_at = NOW()
             WHERE slug = 'pro-monthly-intro'
               AND locale IN ('en-US', 'es-US')
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE messaging_templates
               SET subject = CASE locale
                   WHEN 'en-US' THEN 'Pro Monthly Intro'
                   WHEN 'es-US' THEN 'Pro Mensual'
               END,
                   updated_at = NOW()
             WHERE slug = 'pro-monthly-intro'
               AND locale IN ('en-US', 'es-US')
            """
        )
    )