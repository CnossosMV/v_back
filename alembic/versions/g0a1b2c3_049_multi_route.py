"""049 – Multi-row routing states for concurrent funnels.

Revision ID: g0a1b2c3_049_multi_route
Revises: f9a0b1c2_048_milestone
Create Date: 2026-02-25

Drop single-row UNIQUE constraint, add enrollment_id FK,
create partial unique indexes for funnel vs non-funnel rows.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

revision = "g0a1b2c3_049_multi_route"
down_revision = "f9a0b1c2_048_milestone"
branch_labels = None
depends_on = None


def _constraint_exists(name: str, table: str) -> bool:
    conn = op.get_bind()
    inspector = sa_inspect(conn)
    constraints = inspector.get_unique_constraints(table)
    return any(c["name"] == name for c in constraints)


def _index_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
        {"n": name},
    )
    return result.scalar() is not None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa_inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def upgrade() -> None:
    # 1. Drop old UNIQUE constraint
    if _constraint_exists("uq_routing_state_contact", "contact_routing_states"):
        op.drop_constraint("uq_routing_state_contact", "contact_routing_states", type_="unique")

    # 2. Add enrollment_id FK column
    if not _column_exists("contact_routing_states", "enrollment_id"):
        op.add_column(
            "contact_routing_states",
            sa.Column(
                "enrollment_id",
                sa.Integer(),
                sa.ForeignKey("funnel_enrollments.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )

    # 3. Non-funnel: at most one per contact+channel
    if not _index_exists("uq_routing_non_funnel"):
        op.execute(
            """
            CREATE UNIQUE INDEX uq_routing_non_funnel
            ON contact_routing_states (project_id, contact_identifier, channel)
            WHERE handler_type NOT IN ('funnel_wait', 'funnel_whatsapp')
            """
        )

    # 4. Funnel: at most one per enrollment
    if not _index_exists("uq_routing_funnel_enrollment"):
        op.execute(
            """
            CREATE UNIQUE INDEX uq_routing_funnel_enrollment
            ON contact_routing_states (project_id, contact_identifier, channel, enrollment_id)
            WHERE enrollment_id IS NOT NULL
            """
        )

    # 5. Lookup index for all states of a contact
    if not _index_exists("ix_routing_contact_all"):
        op.create_index(
            "ix_routing_contact_all",
            "contact_routing_states",
            ["project_id", "contact_identifier", "channel"],
        )


def downgrade() -> None:
    # Remove new indexes
    if _index_exists("ix_routing_contact_all"):
        op.drop_index("ix_routing_contact_all", table_name="contact_routing_states")
    if _index_exists("uq_routing_funnel_enrollment"):
        op.execute("DROP INDEX IF EXISTS uq_routing_funnel_enrollment")
    if _index_exists("uq_routing_non_funnel"):
        op.execute("DROP INDEX IF EXISTS uq_routing_non_funnel")

    # Remove enrollment_id column
    if _column_exists("contact_routing_states", "enrollment_id"):
        op.drop_column("contact_routing_states", "enrollment_id")

    # Restore original UNIQUE constraint
    if not _constraint_exists("uq_routing_state_contact", "contact_routing_states"):
        op.create_unique_constraint(
            "uq_routing_state_contact",
            "contact_routing_states",
            ["project_id", "contact_identifier", "channel"],
        )
