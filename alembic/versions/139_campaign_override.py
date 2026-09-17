"""Add auditable campaign pacing overrides.

Revision ID: 139_campaign_override
Revises: 138_campaign_data
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "139_campaign_override"
down_revision = "138_campaign_data"
branch_labels = None
depends_on = None


_TABLE = "campaign_policy_overrides"
_OWNERSHIP_COMMENT = "versya:alembic:139_campaign_override:owned"
_RUN_TABLE = "campaign_runs"
_RUN_SCOPE_CONSTRAINT = "uq_campaign_run_project_id"
_INDEXES = {
    "ix_campaign_policy_overrides_id": ["id"],
    "ix_campaign_policy_overrides_project_id": ["project_id"],
    "ix_campaign_policy_overrides_run_id": ["run_id"],
    "ix_campaign_policy_overrides_status": ["status"],
    "ix_campaign_policy_overrides_expires_at": ["expires_at"],
    "ix_campaign_override_state": ["project_id", "status", "expires_at"],
}
_CONSTRAINTS = {
    "uq_campaign_override_run",
    "fk_campaign_override_run_project",
}


def _table_exists() -> bool:
    return _TABLE in inspect(op.get_bind()).get_table_names()


def _comment(kind: str, name: str | None = None) -> str | None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return None
    if kind == "table":
        return bind.execute(
            sa.text("SELECT obj_description(to_regclass(:target), 'pg_class')"),
            {"target": _TABLE},
        ).scalar()
    if kind == "index":
        return bind.execute(
            sa.text("SELECT obj_description(to_regclass(:target), 'pg_class')"),
            {"target": name},
        ).scalar()
    return bind.execute(sa.text("""
        SELECT obj_description(row.oid, 'pg_constraint')
        FROM pg_constraint AS row
        WHERE row.conrelid = to_regclass(:table)
          AND row.conname = :name
    """), {"table": _TABLE, "name": name}).scalar()


def _mark(kind: str, name: str | None = None) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    comment = _OWNERSHIP_COMMENT.replace("'", "''")
    quoted_table = op.get_bind().dialect.identifier_preparer.quote(_TABLE)
    quoted_name = op.get_bind().dialect.identifier_preparer.quote(str(name))
    if kind == "table":
        op.execute(sa.text(f"COMMENT ON TABLE {quoted_table} IS '{comment}'"))
    elif kind == "index":
        op.execute(sa.text(f"COMMENT ON INDEX {quoted_name} IS '{comment}'"))
    else:
        op.execute(sa.text(
            f"COMMENT ON CONSTRAINT {quoted_name} ON {quoted_table} IS '{comment}'"
        ))


def _constraint_comment(table: str, name: str) -> str | None:
    if op.get_bind().dialect.name != "postgresql":
        return None
    return op.get_bind().execute(sa.text("""
        SELECT obj_description(row.oid, 'pg_constraint')
        FROM pg_constraint AS row
        WHERE row.conrelid = to_regclass(:table)
          AND row.conname = :name
    """), {"table": table, "name": name}).scalar()


def _mark_constraint(table: str, name: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    preparer = op.get_bind().dialect.identifier_preparer
    quoted_table = preparer.quote(table)
    quoted_name = preparer.quote(name)
    comment = _OWNERSHIP_COMMENT.replace("'", "''")
    op.execute(sa.text(
        f"COMMENT ON CONSTRAINT {quoted_name} ON {quoted_table} IS '{comment}'"
    ))


def _ensure_run_scope_constraint() -> None:
    inspector = inspect(op.get_bind())
    constraints = {
        row.get("name"): row
        for row in inspector.get_unique_constraints(_RUN_TABLE)
    }
    existing = constraints.get(_RUN_SCOPE_CONSTRAINT)
    if existing:
        if list(existing.get("column_names") or []) != ["project_id", "id"]:
            raise RuntimeError(
                f"Existing {_RUN_SCOPE_CONSTRAINT} has an incompatible definition"
            )
        if _constraint_comment(_RUN_TABLE, _RUN_SCOPE_CONSTRAINT) != _OWNERSHIP_COMMENT:
            raise RuntimeError(
                f"Refusing to adopt unowned constraint {_RUN_SCOPE_CONSTRAINT}"
            )
        return
    op.create_unique_constraint(
        _RUN_SCOPE_CONSTRAINT,
        _RUN_TABLE,
        ["project_id", "id"],
    )
    _mark_constraint(_RUN_TABLE, _RUN_SCOPE_CONSTRAINT)


def _validate_owned_table() -> None:
    inspector = inspect(op.get_bind())
    required = {
        "id", "project_id", "run_id", "status", "override_keys", "reason",
        "risk_acknowledged", "dual_approval_required", "planned_count_snapshot",
        "policy_snapshot", "requested_by_user_id", "approved_by_user_id",
        "revoked_by_user_id", "requested_at", "approved_at", "expires_at",
        "revoked_at",
    }
    actual = {column["name"] for column in inspector.get_columns(_TABLE)}
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            f"Revision 139 owns {_TABLE}, but required columns are missing: "
            + ", ".join(missing)
        )
    unique_names = {
        item.get("name") for item in inspector.get_unique_constraints(_TABLE)
    }
    foreign_names = {item.get("name") for item in inspector.get_foreign_keys(_TABLE)}
    if "uq_campaign_override_run" not in unique_names:
        raise RuntimeError("Owned campaign override table is missing run uniqueness")
    if "fk_campaign_override_run_project" not in foreign_names:
        raise RuntimeError("Owned campaign override table is missing project-scoped run FK")


def _ensure_indexes() -> None:
    existing = {item["name"] for item in inspect(op.get_bind()).get_indexes(_TABLE)}
    for name, columns in _INDEXES.items():
        if name not in existing:
            op.create_index(name, _TABLE, columns)
            _mark("index", name)


def upgrade() -> None:
    table_exists = _table_exists()
    if table_exists:
        if _comment("table") != _OWNERSHIP_COMMENT:
            raise RuntimeError(
                f"Refusing to adopt pre-existing table {_TABLE}; revision 139 does not own it"
            )

    _ensure_run_scope_constraint()

    if table_exists:
        _validate_owned_table()
        _ensure_indexes()
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="approved", nullable=False),
        sa.Column("override_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("risk_acknowledged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("dual_approval_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("planned_count_snapshot", sa.Integer(), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["campaign_runs.project_id", "campaign_runs.id"],
            name="fk_campaign_override_run_project",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "run_id", name="uq_campaign_override_run"),
    )
    _mark("table")
    for name in _CONSTRAINTS:
        _mark("constraint", name)
    _ensure_indexes()


def downgrade() -> None:
    if _table_exists():
        if _comment("table") != _OWNERSHIP_COMMENT:
            raise RuntimeError(
                f"Refusing to drop unowned table {_TABLE} during revision 139 downgrade"
            )
        op.drop_table(_TABLE)

    constraints = {
        row.get("name")
        for row in inspect(op.get_bind()).get_unique_constraints(_RUN_TABLE)
    }
    if _RUN_SCOPE_CONSTRAINT in constraints:
        if _constraint_comment(_RUN_TABLE, _RUN_SCOPE_CONSTRAINT) != _OWNERSHIP_COMMENT:
            raise RuntimeError(
                f"Refusing to drop unowned constraint {_RUN_SCOPE_CONSTRAINT}"
            )
        op.drop_constraint(_RUN_SCOPE_CONSTRAINT, _RUN_TABLE, type_="unique")
