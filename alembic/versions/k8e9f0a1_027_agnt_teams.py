"""Add agent teams tables

Revision ID: k8e9f0a1_027_agnt_teams
Revises: e7f8a9b0_026_evt_actions
Create Date: 2026-02-08 10:00:00.000000

Creates tables for the Agent Teams orchestration system:
- agent_teams: Team configuration with router + specialists
- specialist_agents: Individual specialist agents with persona
- specialist_knowledge_sources: Knowledge sources per specialist
- router_configs: Router configuration per team
- routing_rules: Rules for message routing

Note: Made idempotent to handle potential migration state inconsistencies.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, ARRAY as PG_ARRAY
from sqlalchemy import inspect


revision = 'k8e9f0a1_027_agnt_teams'
down_revision = 'e7f8a9b0_026_evt_actions'
branch_labels = None
depends_on = None


def table_exists(table_name: str) -> bool:
    """Check if a table already exists in the database."""
    connection = op.get_bind()
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # Check if tables already exist (idempotent migration)
    if table_exists('agent_teams'):
        return

    # Create agent_teams table
    op.create_table(
        'agent_teams',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), server_default='draft', nullable=False),
        sa.Column('deployment_channels', JSON, server_default='[]', nullable=True),
        sa.Column('initial_message', sa.Text(), nullable=True),
        sa.Column('whatsapp_instance_id', sa.Integer(), nullable=True),
        sa.Column('auto_respond_whatsapp', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_public', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('migrated_from_chatbot_id', sa.Integer(), nullable=True),
        sa.Column('team_metadata', JSON, server_default='{}', nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['whatsapp_instance_id'], ['whatsapp_instances.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['migrated_from_chatbot_id'], ['chatbots.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.CheckConstraint("status IN ('draft', 'active', 'paused', 'archived')", name='valid_team_status'),
        sa.UniqueConstraint('migrated_from_chatbot_id', name='uq_agent_teams_migrated_chatbot')
    )
    op.create_index('ix_agent_teams_id', 'agent_teams', ['id'])
    op.create_index('ix_agent_teams_project_id', 'agent_teams', ['project_id'])
    op.create_index('ix_agent_teams_status', 'agent_teams', ['status'])

    # Create specialist_agents table
    op.create_table(
        'specialist_agents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('icon', sa.String(10), nullable=True),
        sa.Column('is_default', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('position_x', sa.Float(), server_default='0.0', nullable=False),
        sa.Column('position_y', sa.Float(), server_default='0.0', nullable=False),
        # Persona
        sa.Column('system_prompt', sa.Text(), nullable=True),
        sa.Column('model_provider', sa.String(50), server_default='openai', nullable=False),
        sa.Column('model_name', sa.String(100), server_default='gpt-4o-mini', nullable=False),
        sa.Column('temperature', sa.Float(), server_default='0.7'),
        sa.Column('max_tokens', sa.Integer(), server_default='2000'),
        sa.Column('tone', sa.String(30), server_default='friendly', nullable=True),
        sa.Column('language', sa.String(10), nullable=True),
        # Guardrails
        sa.Column('max_turns', sa.Integer(), nullable=True),
        sa.Column('frustration_action', sa.String(20), server_default='escalate', nullable=True),
        sa.Column('blocked_topics', PG_ARRAY(sa.String), server_default='{}', nullable=True),
        sa.Column('operating_hours', JSON, nullable=True),
        sa.Column('escalation_config', JSON, nullable=True),
        # Storage
        sa.Column('vector_store_path', sa.String(500), nullable=True),
        sa.Column('knowledge_base_path', sa.String(500), nullable=True),
        # Status
        sa.Column('status', sa.String(20), server_default='active', nullable=False),
        sa.Column('agent_order', sa.Integer(), server_default='0', nullable=False),
        # Timestamps
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['team_id'], ['agent_teams.id'], ondelete='CASCADE')
    )
    op.create_index('ix_specialist_agents_id', 'specialist_agents', ['id'])
    op.create_index('ix_specialist_agents_team_id', 'specialist_agents', ['team_id'])
    op.create_index('ix_specialist_agents_status', 'specialist_agents', ['status'])

    # Create specialist_knowledge_sources table
    op.create_table(
        'specialist_knowledge_sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('specialist_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(50), nullable=False),
        sa.Column('source_url', sa.String(1000), nullable=True),
        sa.Column('file_path', sa.String(500), nullable=True),
        sa.Column('file_name', sa.String(200), nullable=True),
        sa.Column('file_type', sa.String(50), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('chunk_count', sa.Integer(), server_default='0'),
        sa.Column('embedding_model', sa.String(100), nullable=True),
        sa.Column('expose_to_user', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('label', sa.String(200), nullable=True),
        sa.Column('processed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('processing_error', sa.Text(), nullable=True),
        sa.Column('source_metadata', JSON, server_default='{}', nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['specialist_id'], ['specialist_agents.id'], ondelete='CASCADE')
    )
    op.create_index('ix_spec_knowledge_id', 'specialist_knowledge_sources', ['id'])
    op.create_index('ix_spec_knowledge_spec_id', 'specialist_knowledge_sources', ['specialist_id'])
    op.create_index('ix_spec_knowledge_type', 'specialist_knowledge_sources', ['source_type'])
    op.create_index('ix_spec_knowledge_hash', 'specialist_knowledge_sources', ['content_hash'])
    op.create_index('ix_spec_knowledge_processed', 'specialist_knowledge_sources', ['processed'])

    # Create router_configs table
    op.create_table(
        'router_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('routing_mode', sa.String(20), server_default='auto', nullable=False),
        sa.Column('model_provider', sa.String(50), server_default='openai', nullable=False),
        sa.Column('model_name', sa.String(100), server_default='gpt-4o-mini', nullable=False),
        sa.Column('default_agent_id', sa.Integer(), nullable=True),
        sa.Column('allow_mid_convo_switch', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('switch_notification', sa.String(20), server_default='seamless', nullable=False),
        sa.Column('initial_message', sa.Text(), nullable=True),
        sa.Column('position_x', sa.Float(), server_default='300.0', nullable=False),
        sa.Column('position_y', sa.Float(), server_default='200.0', nullable=False),
        sa.Column('router_metadata', JSON, server_default='{}', nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['team_id'], ['agent_teams.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['default_agent_id'], ['specialist_agents.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('team_id', name='uq_router_configs_team'),
        sa.CheckConstraint("routing_mode IN ('auto', 'rules', 'hybrid')", name='valid_routing_mode')
    )
    op.create_index('ix_router_configs_id', 'router_configs', ['id'])
    op.create_index('ix_router_configs_team_id', 'router_configs', ['team_id'])

    # Create routing_rules table
    op.create_table(
        'routing_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('router_id', sa.Integer(), nullable=False),
        sa.Column('specialist_id', sa.Integer(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
        sa.Column('keyword_hints', PG_ARRAY(sa.String), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['router_id'], ['router_configs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['specialist_id'], ['specialist_agents.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('router_id', 'specialist_id', name='uq_routing_rule_router_specialist')
    )
    op.create_index('ix_routing_rules_id', 'routing_rules', ['id'])
    op.create_index('ix_routing_rules_router_id', 'routing_rules', ['router_id'])
    op.create_index('ix_routing_rules_spec_id', 'routing_rules', ['specialist_id'])


def downgrade() -> None:
    op.drop_table('routing_rules')
    op.drop_table('router_configs')
    op.drop_table('specialist_knowledge_sources')
    op.drop_table('specialist_agents')
    op.drop_table('agent_teams')
