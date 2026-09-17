"""048 – JSONB functional index for paused enrollment queries.

Revision ID: f9a0b1c2_048_milestone
Revises: e8f9a0b1_047_wa_cc
Create Date: 2026-02-25
"""

from alembic import op

revision = "f9a0b1c2_048_milestone"
down_revision = "e8f9a0b1_047_wa_cc"
branch_labels = None
depends_on = None


def _index_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        __import__("sqlalchemy").text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :n"
        ),
        {"n": name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _index_exists("ix_enrollment_paused"):
        op.execute(
            """
            CREATE INDEX ix_enrollment_paused
            ON funnel_enrollments ((enrollment_metadata->>'_is_paused'))
            WHERE status = 'active'
            """
        )


def downgrade() -> None:
    if _index_exists("ix_enrollment_paused"):
        op.execute("DROP INDEX ix_enrollment_paused")
