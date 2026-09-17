"""Add persistent campaign recipes and planned episodes.

Revision ID: 152_campaign_recipes
Revises: 151_future_attention_planning
Create Date: 2026-08-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "152_campaign_recipes"
down_revision = "151_future_attention_planning"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _index_exists(table: str, name: str) -> bool:
    return any(item["name"] == name for item in inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _table_exists("campaign_recipes"):
        op.create_table(
            "campaign_recipes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("lifecycle_model_id", sa.Integer(), sa.ForeignKey("lifecycle_models.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("external_key", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("purpose_key", sa.String(120), nullable=False),
            sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
            sa.Column("country_code", sa.String(2), nullable=True),
            sa.Column("region_code", sa.String(80), nullable=True),
            sa.Column("default_channel", sa.String(50), nullable=False, server_default="email"),
            sa.Column("selection_config", postgresql.JSONB(), nullable=False),
            sa.Column("policy_config", postgresql.JSONB(), nullable=True),
            sa.Column("attention_policy", postgresql.JSONB(), nullable=False),
            sa.Column("schedule_rules", postgresql.JSONB(), nullable=False),
            sa.Column("include_persisted_opportunities", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("content_mode", sa.String(30), nullable=False, server_default="agent_draft"),
            sa.Column("content_brief", postgresql.JSONB(), nullable=False),
            sa.Column("fixed_actions", postgresql.JSONB(), nullable=True),
            sa.Column("autonomy_policy", postgresql.JSONB(), nullable=False),
            sa.Column("planning_horizon_days", sa.Integer(), nullable=False, server_default="45"),
            sa.Column("decision_lead_hours", sa.Integer(), nullable=False, server_default="168"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("last_materialized_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "external_key", name="uq_campaign_recipe_external"),
            sa.CheckConstraint("status IN ('draft','active','paused','archived')", name="ck_campaign_recipe_status"),
            sa.CheckConstraint(
                "content_mode IN ('fixed','agent_draft','bounded_autonomy')",
                name="ck_campaign_recipe_content_mode",
            ),
        )
    for name, columns in (
        ("ix_campaign_recipes_project_id", ["project_id"]),
        ("ix_campaign_recipes_lifecycle_model_id", ["lifecycle_model_id"]),
        ("ix_campaign_recipes_status", ["status"]),
        ("ix_campaign_recipes_purpose_key", ["purpose_key"]),
        ("ix_campaign_recipe_project_status", ["project_id", "status"]),
    ):
        if not _index_exists("campaign_recipes", name):
            op.create_index(name, "campaign_recipes", columns)

    if not _table_exists("campaign_recipe_episodes"):
        op.create_table(
            "campaign_recipe_episodes",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("campaign_recipes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("recipe_version", sa.Integer(), nullable=False),
            sa.Column("occurrence_key", sa.String(255), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="awaiting_copy"),
            sa.Column("opportunity_snapshot", postgresql.JSONB(), nullable=False),
            sa.Column("collision_snapshot", postgresql.JSONB(), nullable=True),
            sa.Column("collision_resolution", postgresql.JSONB(), nullable=True),
            sa.Column("content_brief_snapshot", postgresql.JSONB(), nullable=False),
            sa.Column("decision_due_at", sa.DateTime(), nullable=False),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("target_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True),
            sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("campaign_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("authored_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("materialized_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("recipe_id", "occurrence_key", name="uq_campaign_recipe_occurrence"),
            sa.UniqueConstraint("project_id", "campaign_id", name="uq_campaign_episode_campaign"),
            sa.CheckConstraint(
                "status IN ('awaiting_copy','ready','blocked','materialized','scheduled','completed','canceled','failed','expired','superseded')",
                name="ck_campaign_recipe_episode_status",
            ),
        )
    for name, columns in (
        ("ix_campaign_recipe_episodes_project_id", ["project_id"]),
        ("ix_campaign_recipe_episodes_recipe_id", ["recipe_id"]),
        ("ix_campaign_recipe_episodes_status", ["status"]),
        ("ix_campaign_recipe_episodes_decision_due_at", ["decision_due_at"]),
        ("ix_campaign_recipe_episodes_starts_at", ["starts_at"]),
        ("ix_campaign_recipe_episodes_expires_at", ["expires_at"]),
        ("ix_campaign_recipe_episodes_campaign_id", ["campaign_id"]),
        ("ix_campaign_recipe_episodes_run_id", ["run_id"]),
        ("ix_campaign_recipe_episode_inbox", ["project_id", "status", "decision_due_at", "starts_at"]),
    ):
        if not _index_exists("campaign_recipe_episodes", name):
            op.create_index(name, "campaign_recipe_episodes", columns)


def downgrade() -> None:
    if _table_exists("campaign_recipe_episodes"):
        op.drop_table("campaign_recipe_episodes")
    if _table_exists("campaign_recipes"):
        op.drop_table("campaign_recipes")
