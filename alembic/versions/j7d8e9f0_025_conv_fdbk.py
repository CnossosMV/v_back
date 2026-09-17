"""024_add_conversation_feedback

Revision ID: j7d8e9f0g1h2
Revises: i6c7d8e9f0g1
Create Date: 2026-01-19 10:15:00.000000

Adds unified chat system models - Phase 4:
- ConversationFeedback (Training Data Collection)
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'j7d8e9f0g1h2'
down_revision = 'i6c7d8e9f0g1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create conversation_feedback table
    op.create_table(
        'conversation_feedback',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),  # Specific message feedback (optional)

        # Rating
        sa.Column('rating', sa.String(20), nullable=True),  # thumbs_up, thumbs_down
        sa.Column('rating_score', sa.Integer(), nullable=True),  # 1-5 scale
        sa.Column('feedback_text', sa.Text(), nullable=True),  # Optional text feedback

        # Training flags
        sa.Column('is_helpful', sa.Boolean(), nullable=True),
        sa.Column('should_be_faq', sa.Boolean(), default=False, nullable=False),  # Mark for FAQ extraction

        # FAQ extraction fields
        sa.Column('extracted_question', sa.Text(), nullable=True),
        sa.Column('extracted_answer', sa.Text(), nullable=True),
        sa.Column('faq_category', sa.String(100), nullable=True),

        # Review status
        sa.Column('reviewed', sa.Boolean(), default=False, nullable=False),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('review_notes', sa.Text(), nullable=True),

        # Submitter info
        sa.Column('submitted_by_user_id', sa.Integer(), nullable=True),
        sa.Column('submitter_identifier', sa.String(255), nullable=True),  # For anonymous submissions

        # Metadata
        sa.Column('feedback_metadata', sa.JSON(), default={}, nullable=True),

        # Timestamps
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),

        sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['message_id'], ['chat_messages.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['submitted_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "rating IS NULL OR rating IN ('thumbs_up', 'thumbs_down')",
            name='valid_feedback_rating'
        ),
        sa.CheckConstraint(
            "rating_score IS NULL OR (rating_score >= 1 AND rating_score <= 5)",
            name='valid_rating_score'
        )
    )

    # Create indexes for conversation_feedback
    op.create_index('ix_conversation_feedback_session', 'conversation_feedback', ['session_id'])
    op.create_index('ix_conversation_feedback_message', 'conversation_feedback', ['message_id'])
    op.create_index('ix_conversation_feedback_should_faq', 'conversation_feedback', ['should_be_faq'])
    op.create_index('ix_conversation_feedback_reviewed', 'conversation_feedback', ['reviewed'])
    op.create_index('ix_conversation_feedback_rating', 'conversation_feedback', ['rating'])
    op.create_index('ix_conversation_feedback_created', 'conversation_feedback', ['created_at'])


def downgrade() -> None:
    # Drop indexes and table
    op.drop_index('ix_conversation_feedback_created', table_name='conversation_feedback')
    op.drop_index('ix_conversation_feedback_rating', table_name='conversation_feedback')
    op.drop_index('ix_conversation_feedback_reviewed', table_name='conversation_feedback')
    op.drop_index('ix_conversation_feedback_should_faq', table_name='conversation_feedback')
    op.drop_index('ix_conversation_feedback_message', table_name='conversation_feedback')
    op.drop_index('ix_conversation_feedback_session', table_name='conversation_feedback')
    op.drop_table('conversation_feedback')
