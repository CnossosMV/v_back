"""Add playbooks and analytics tables

Revision ID: o2c3d4e5_031_plybk_anlyt
Revises: n1b2c3d4_030_spec_tools
Create Date: 2026-02-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = 'o2c3d4e5_031_plybk_anlyt'
down_revision = 'n1b2c3d4_030_spec_tools'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # -- team_playbooks --
    if not table_exists("team_playbooks"):
        op.create_table(
            "team_playbooks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("slug", sa.String(100), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("category", sa.String(50), nullable=False),
            sa.Column("icon", sa.String(10), nullable=True),
            sa.Column("template_data", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("agent_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_team_playbooks_id", "team_playbooks", ["id"])
        op.create_index("ix_team_playbooks_slug", "team_playbooks", ["slug"], unique=True)
        op.create_index("ix_team_playbooks_category", "team_playbooks", ["category"])

    # -- team_analytics_snapshots --
    if not table_exists("team_analytics_snapshots"):
        op.create_table(
            "team_analytics_snapshots",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("total_sessions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("resolved_sessions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("escalated_sessions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("avg_turns_to_resolve", sa.Float(), nullable=True),
            sa.Column("specialist_distribution", sa.JSON(), nullable=True),
            sa.Column("routing_accuracy", sa.Float(), nullable=True),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tool_success_rate", sa.Float(), nullable=True),
            sa.Column("avg_response_time_ms", sa.Integer(), nullable=True),
            sa.Column("snapshot_metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["team_id"], ["agent_teams.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_team_analytics_snapshots_id", "team_analytics_snapshots", ["id"])
        op.create_index("ix_team_analytics_snapshots_team_id", "team_analytics_snapshots", ["team_id"])
        op.create_index(
            "ix_team_analytics_team_period",
            "team_analytics_snapshots",
            ["team_id", "period_start"],
        )

    # -- Seed initial playbooks --
    op.execute("""
        INSERT INTO team_playbooks (name, slug, description, category, icon, template_data, agent_count, is_active, display_order)
        SELECT * FROM (VALUES
            ('SaaS B2B Support', 'saas-b2b', 'Pre-configured team for SaaS companies with sales, support, and onboarding specialists.', 'saas', '💼',
             '{"specialists": [{"name": "Sales Assistant", "icon": "💰", "system_prompt": "[PLACEHOLDER] You are a sales assistant...", "tone": "friendly"}, {"name": "Technical Support", "icon": "🔧", "system_prompt": "[PLACEHOLDER] You are a technical support agent...", "tone": "direct"}, {"name": "Onboarding Guide", "icon": "🎯", "system_prompt": "[PLACEHOLDER] You are an onboarding specialist...", "tone": "empathetic"}], "router": {"routing_mode": "auto", "model_name": "gpt-4o-mini"}}'::json,
             3, true, 1),
            ('E-commerce Store', 'ecommerce', 'Team for online stores with order tracking, product recommendations, and returns handling.', 'ecommerce', '🛒',
             '{"specialists": [{"name": "Order Tracker", "icon": "📦", "system_prompt": "[PLACEHOLDER] You help customers track orders...", "tone": "friendly"}, {"name": "Product Advisor", "icon": "🎁", "system_prompt": "[PLACEHOLDER] You recommend products...", "tone": "friendly"}, {"name": "Returns & Refunds", "icon": "↩️", "system_prompt": "[PLACEHOLDER] You handle returns and refunds...", "tone": "empathetic"}], "router": {"routing_mode": "hybrid", "model_name": "gpt-4o-mini"}}'::json,
             3, true, 2),
            ('Healthcare Clinic', 'healthcare', 'Team for clinics with appointment scheduling, FAQ, and triage specialists.', 'healthcare', '🏥',
             '{"specialists": [{"name": "Appointment Scheduler", "icon": "📅", "system_prompt": "[PLACEHOLDER] You help schedule appointments...", "tone": "formal"}, {"name": "Health FAQ", "icon": "❓", "system_prompt": "[PLACEHOLDER] You answer common health questions...", "tone": "empathetic"}, {"name": "Triage Assistant", "icon": "🩺", "system_prompt": "[PLACEHOLDER] You help assess urgency...", "tone": "empathetic"}], "router": {"routing_mode": "auto", "model_name": "gpt-4o-mini"}}'::json,
             3, true, 3),
            ('Online Course', 'education', 'Team for educational platforms with enrollment help, content guidance, and technical support.', 'education', '📚',
             '{"specialists": [{"name": "Enrollment Advisor", "icon": "🎓", "system_prompt": "[PLACEHOLDER] You help students enroll...", "tone": "friendly"}, {"name": "Course Guide", "icon": "📖", "system_prompt": "[PLACEHOLDER] You help navigate course content...", "tone": "friendly"}, {"name": "Tech Support", "icon": "💻", "system_prompt": "[PLACEHOLDER] You resolve technical issues...", "tone": "direct"}], "router": {"routing_mode": "auto", "model_name": "gpt-4o-mini"}}'::json,
             3, true, 4),
            ('General Customer Service', 'general-cs', 'Versatile team with a generalist agent and escalation specialist.', 'general', '🌐',
             '{"specialists": [{"name": "General Assistant", "icon": "🤖", "system_prompt": "[PLACEHOLDER] You are a general customer service assistant...", "tone": "friendly", "is_default": true}, {"name": "Escalation Specialist", "icon": "🆘", "system_prompt": "[PLACEHOLDER] You handle complex or escalated issues...", "tone": "empathetic"}], "router": {"routing_mode": "auto", "model_name": "gpt-4o-mini"}}'::json,
             2, true, 5)
        ) AS v(name, slug, description, category, icon, template_data, agent_count, is_active, display_order)
        WHERE NOT EXISTS (SELECT 1 FROM team_playbooks LIMIT 1);
    """)


def downgrade() -> None:
    op.drop_table("team_analytics_snapshots")
    op.drop_table("team_playbooks")
