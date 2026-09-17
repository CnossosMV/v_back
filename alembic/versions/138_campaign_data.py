"""Add contact, campaign and provider-neutral delivery data.

Revision ID: 138_campaign_data
Revises: 137_contact_verify
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "138_campaign_data"
down_revision = "137_contact_verify"
branch_labels = None
depends_on = None


_OWNERSHIP_COMMENT = "versya:alembic:138_campaign_data:owned"


_NEW_TABLES = (
    "contact_endpoints",
    "contact_permission_evidence",
    "contact_groups",
    "contact_group_memberships",
    "campaigns",
    "campaign_actions",
    "campaign_variants",
    "campaign_runs",
    "campaign_waves",
    "channel_sender_identities",
    "channel_delivery_profiles",
    "campaign_recipients",
    "channel_capacity_reservations",
    "contact_verification_requests",
    "operational_alerts",
)


# Redundant (project_id, id) keys are intentional: they let every tenant-owned
# relationship be protected by a composite FK, so malformed/raw SQL cannot
# connect rows from different projects while application-level RBAC is bypassed.
_PROJECT_PARENT_UNIQUES = (
    ("uq_projects_ws_id", "projects", ("workspace_id", "id")),
    ("uq_msg_users_proj_id", "messaging_users", ("project_id", "id")),
    ("uq_msg_templates_proj_id", "messaging_templates", ("project_id", "id")),
    ("uq_send_logs_proj_id", "send_logs", ("project_id", "id")),
    ("uq_verify_jobs_proj_id", "contact_verification_jobs", ("project_id", "id")),
    ("uq_contact_endpoints_proj_id", "contact_endpoints", ("project_id", "id")),
    ("uq_contact_groups_proj_id", "contact_groups", ("project_id", "id")),
    ("uq_campaigns_proj_id", "campaigns", ("project_id", "id")),
    ("uq_campaign_actions_proj_id", "campaign_actions", ("project_id", "id")),
    ("uq_campaign_variants_proj_id", "campaign_variants", ("project_id", "id")),
    ("uq_campaign_runs_proj_id", "campaign_runs", ("project_id", "id")),
    ("uq_campaign_waves_proj_id", "campaign_waves", ("project_id", "id")),
    ("uq_sender_identities_proj_id", "channel_sender_identities", ("project_id", "id")),
    ("uq_delivery_profiles_proj_id", "channel_delivery_profiles", ("project_id", "id")),
)


_PROJECT_SCOPED_FOREIGN_KEYS = (
    ("fk_endpoint_user_project", "contact_endpoints", ("project_id", "user_id"), "messaging_users", ("project_id", "id")),
    ("fk_permission_user_project", "contact_permission_evidence", ("project_id", "user_id"), "messaging_users", ("project_id", "id")),
    ("fk_permission_endpoint_project", "contact_permission_evidence", ("project_id", "endpoint_id"), "contact_endpoints", ("project_id", "id")),
    ("fk_group_member_group_project", "contact_group_memberships", ("project_id", "group_id"), "contact_groups", ("project_id", "id")),
    ("fk_group_member_user_project", "contact_group_memberships", ("project_id", "user_id"), "messaging_users", ("project_id", "id")),
    ("fk_campaign_action_campaign_project", "campaign_actions", ("project_id", "campaign_id"), "campaigns", ("project_id", "id")),
    ("fk_campaign_variant_campaign_project", "campaign_variants", ("project_id", "campaign_id"), "campaigns", ("project_id", "id")),
    ("fk_campaign_variant_action_project", "campaign_variants", ("project_id", "action_id"), "campaign_actions", ("project_id", "id")),
    ("fk_campaign_variant_template_project", "campaign_variants", ("project_id", "template_id"), "messaging_templates", ("project_id", "id")),
    ("fk_campaign_run_campaign_project", "campaign_runs", ("project_id", "campaign_id"), "campaigns", ("project_id", "id")),
    ("fk_campaign_wave_run_project", "campaign_waves", ("project_id", "run_id"), "campaign_runs", ("project_id", "id")),
    ("fk_delivery_profile_sender_project", "channel_delivery_profiles", ("project_id", "sender_identity_id"), "channel_sender_identities", ("project_id", "id")),
    ("fk_campaign_recipient_run_project", "campaign_recipients", ("project_id", "run_id"), "campaign_runs", ("project_id", "id")),
    ("fk_campaign_recipient_wave_project", "campaign_recipients", ("project_id", "wave_id"), "campaign_waves", ("project_id", "id")),
    ("fk_campaign_recipient_action_project", "campaign_recipients", ("project_id", "action_id"), "campaign_actions", ("project_id", "id")),
    ("fk_campaign_recipient_variant_project", "campaign_recipients", ("project_id", "variant_id"), "campaign_variants", ("project_id", "id")),
    ("fk_campaign_recipient_user_project", "campaign_recipients", ("project_id", "user_id"), "messaging_users", ("project_id", "id")),
    ("fk_campaign_recipient_endpoint_project", "campaign_recipients", ("project_id", "endpoint_id"), "contact_endpoints", ("project_id", "id")),
    ("fk_campaign_recipient_profile_project", "campaign_recipients", ("project_id", "delivery_profile_id"), "channel_delivery_profiles", ("project_id", "id")),
    ("fk_campaign_recipient_sender_project", "campaign_recipients", ("project_id", "sender_identity_id"), "channel_sender_identities", ("project_id", "id")),
    ("fk_campaign_recipient_sendlog_project", "campaign_recipients", ("project_id", "send_log_id"), "send_logs", ("project_id", "id")),
    ("fk_capacity_profile_project", "channel_capacity_reservations", ("project_id", "delivery_profile_id"), "channel_delivery_profiles", ("project_id", "id")),
    ("fk_capacity_run_project", "channel_capacity_reservations", ("project_id", "run_id"), "campaign_runs", ("project_id", "id")),
    ("fk_capacity_wave_project", "channel_capacity_reservations", ("project_id", "wave_id"), "campaign_waves", ("project_id", "id")),
    ("fk_verify_request_user_project", "contact_verification_requests", ("project_id", "user_id"), "messaging_users", ("project_id", "id")),
    ("fk_verify_request_endpoint_project", "contact_verification_requests", ("project_id", "endpoint_id"), "contact_endpoints", ("project_id", "id")),
    ("fk_verify_request_job_project", "contact_verification_requests", ("project_id", "job_id"), "contact_verification_jobs", ("project_id", "id")),
    ("fk_verify_state_user_project", "contact_verification_states", ("project_id", "user_id"), "messaging_users", ("project_id", "id")),
    ("fk_verify_state_endpoint_project", "contact_verification_states", ("project_id", "endpoint_id"), "contact_endpoints", ("project_id", "id")),
    ("fk_verify_job_endpoint_project", "contact_verification_jobs", ("project_id", "endpoint_id"), "contact_endpoints", ("project_id", "id")),
    ("fk_verify_item_job_project", "contact_verification_items", ("project_id", "job_id"), "contact_verification_jobs", ("project_id", "id")),
    ("fk_verify_item_user_project", "contact_verification_items", ("project_id", "user_id"), "messaging_users", ("project_id", "id")),
    ("fk_verify_item_endpoint_project", "contact_verification_items", ("project_id", "endpoint_id"), "contact_endpoints", ("project_id", "id")),
    ("fk_verify_operation_job_project", "contact_verification_operations", ("project_id", "job_id"), "contact_verification_jobs", ("project_id", "id")),
    ("fk_verify_operation_endpoint_project", "contact_verification_operations", ("project_id", "endpoint_id"), "contact_endpoints", ("project_id", "id")),
    ("fk_operational_alert_project_workspace", "operational_alerts", ("workspace_id", "project_id"), "projects", ("workspace_id", "id")),
)


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _column_exists(table: str, column: str) -> bool:
    if not _table_exists(table):
        return False
    return column in {item["name"] for item in inspect(op.get_bind()).get_columns(table)}


def _index_exists(table: str, index: str) -> bool:
    if not _table_exists(table):
        return False
    return index in {item["name"] for item in inspect(op.get_bind()).get_indexes(table)}


def _constraint_exists(table: str, constraint: str) -> bool:
    if not _table_exists(table):
        return False
    inspector = inspect(op.get_bind())
    return any(item.get("name") == constraint for item in inspector.get_unique_constraints(table))


def _quoted(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _object_comment(kind: str, table: str, name: str | None = None) -> str | None:
    """Read PostgreSQL ownership comments used for fail-safe downgrade.

    Revision 138 is PostgreSQL-specific (JSONB, partial indexes and SKIP
    LOCKED workers).  Comments let the downgrade distinguish objects created
    here from a same-named object that existed before this revision.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return None
    if kind in {"table", "index"}:
        target = table if kind == "table" else str(name)
        return bind.execute(
            sa.text("SELECT obj_description(to_regclass(:target), 'pg_class')"),
            {"target": target},
        ).scalar()
    if kind == "column":
        return bind.execute(sa.text("""
            SELECT col_description(attribute.attrelid, attribute.attnum)
            FROM pg_attribute AS attribute
            WHERE attribute.attrelid = to_regclass(:table)
              AND attribute.attname = :name
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
        """), {"table": table, "name": name}).scalar()
    if kind == "constraint":
        return bind.execute(sa.text("""
            SELECT obj_description(constraint_row.oid, 'pg_constraint')
            FROM pg_constraint AS constraint_row
            WHERE constraint_row.conrelid = to_regclass(:table)
              AND constraint_row.conname = :name
        """), {"table": table, "name": name}).scalar()
    raise ValueError(f"Unsupported migration object kind: {kind}")


def _mark_owned(kind: str, table: str, name: str | None = None) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    table_sql = _quoted(table)
    comment_sql = _OWNERSHIP_COMMENT.replace("'", "''")
    if kind == "table":
        op.execute(sa.text(f"COMMENT ON TABLE {table_sql} IS '{comment_sql}'"))
    elif kind == "index":
        op.execute(sa.text(
            f"COMMENT ON INDEX {_quoted(str(name))} IS '{comment_sql}'"
        ))
    elif kind == "column":
        op.execute(sa.text(
            f"COMMENT ON COLUMN {table_sql}.{_quoted(str(name))} IS '{comment_sql}'"
        ))
    elif kind == "constraint":
        op.execute(sa.text(
            f"COMMENT ON CONSTRAINT {_quoted(str(name))} ON {table_sql} IS '{comment_sql}'"
        ))
    else:
        raise ValueError(f"Unsupported migration object kind: {kind}")


def _is_owned(kind: str, table: str, name: str | None = None) -> bool:
    return _object_comment(kind, table, name) == _OWNERSHIP_COMMENT


def _foreign_key_exists(table: str, columns: set[str], referred_table: str) -> bool:
    if not _table_exists(table):
        return False
    return any(
        set(item.get("constrained_columns") or []) == columns
        and item.get("referred_table") == referred_table
        for item in inspect(op.get_bind()).get_foreign_keys(table)
    )


def _create_index(name: str, table: str, columns: list[str], **kwargs) -> None:
    if not _table_exists(table):
        return
    if _index_exists(table, name):
        return
    op.create_index(name, table, columns, **kwargs)
    _mark_owned("index", table, name)


def _validate_owned_table_shape(name: str, elements: tuple) -> None:
    """Fail explicitly instead of accepting an incomplete partial table."""
    inspector = inspect(op.get_bind())
    actual_columns = {item["name"] for item in inspector.get_columns(name)}
    expected_columns = {
        element.name for element in elements if isinstance(element, sa.Column)
    }
    missing_columns = sorted(expected_columns - actual_columns)
    if missing_columns:
        raise RuntimeError(
            f"Revision 138 owns {name}, but required columns are missing: "
            f"{', '.join(missing_columns)}"
        )

    expected_pk = {
        element.name
        for element in elements
        if isinstance(element, sa.Column) and element.primary_key
    }
    actual_pk = set(inspector.get_pk_constraint(name).get("constrained_columns") or [])
    if expected_pk and actual_pk != expected_pk:
        raise RuntimeError(f"Revision 138 owns {name}, but its primary key is incomplete")

    actual_uniques = {
        (item.get("name"), frozenset(item.get("column_names") or []))
        for item in inspector.get_unique_constraints(name)
    }
    for element in elements:
        if isinstance(element, sa.UniqueConstraint):
            pending = getattr(element, "_pending_colargs", ())
            expected_columns = frozenset(
                item if isinstance(item, str) else item.name
                for item in pending
            )
            expected = (element.name, expected_columns)
            if expected not in actual_uniques:
                raise RuntimeError(
                    f"Revision 138 owns {name}, but unique constraint {element.name} is missing"
                )
        if isinstance(element, sa.ForeignKeyConstraint):
            pending = getattr(element, "_pending_colargs", ())
            local_columns = {
                item if isinstance(item, str) else item.name
                for item in pending
            }
            target_table = element.elements[0].target_fullname.rsplit(".", 1)[0]
            if not _foreign_key_exists(name, local_columns, target_table):
                raise RuntimeError(
                    f"Revision 138 owns {name}, but a foreign key to {target_table} is missing"
                )


def _create_table(
    name: str,
    *columns,
    allow_owned_partial: bool = False,
    **kwargs,
) -> None:
    if _table_exists(name):
        if not _is_owned("table", name):
            raise RuntimeError(
                f"Refusing to adopt pre-existing table {name}; revision 138 does not own it"
            )
        if not allow_owned_partial:
            _validate_owned_table_shape(name, columns)
        return
    op.create_table(name, *columns, **kwargs)
    _mark_owned("table", name)


def _add_owned_column(table: str, column: sa.Column) -> bool:
    if _column_exists(table, column.name):
        return False
    op.add_column(table, column)
    _mark_owned("column", table, column.name)
    return True


def _ensure_unique_constraint(name: str, table: str, columns: tuple[str, ...]) -> None:
    if (
        _table_exists(table)
        and all(_column_exists(table, column) for column in columns)
        and not _constraint_exists(table, name)
    ):
        op.create_unique_constraint(name, table, list(columns))
        _mark_owned("constraint", table, name)


def _ensure_scoped_foreign_key(
    name: str,
    table: str,
    columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
) -> None:
    if (
        _table_exists(table)
        and _table_exists(referred_table)
        and all(_column_exists(table, column) for column in columns)
        and all(_column_exists(referred_table, column) for column in referred_columns)
        and not _foreign_key_exists(table, set(columns), referred_table)
    ):
        # Existing single-column FKs retain their CASCADE/SET NULL behavior.
        # This additional constraint exists solely to prove tenant equality and
        # therefore uses the default NO ACTION semantics.
        op.create_foreign_key(
            name,
            table,
            referred_table,
            list(columns),
            list(referred_columns),
        )
        _mark_owned("constraint", table, name)


def upgrade() -> None:
    # Contact endpoints are created first because verification, permission and
    # campaign work-item tables all reference them.
    _create_table(
        "contact_endpoints",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_type", sa.String(30), nullable=False),
        sa.Column("value", sa.String(500), nullable=False),
        sa.Column("normalized_value", sa.String(500), nullable=True),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        sa.Column("source", sa.String(50), server_default="migration", nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "user_id", "endpoint_type", "value_hash", name="uq_contact_endpoint_hash"),
    )
    _create_index("ix_contact_endpoints_project_id", "contact_endpoints", ["project_id"])
    _create_index("ix_contact_endpoints_user_id", "contact_endpoints", ["user_id"])
    _create_index("ix_contact_endpoint_type", "contact_endpoints", ["endpoint_type"])
    _create_index("ix_contact_endpoint_value_hash", "contact_endpoints", ["value_hash"])
    _create_index("ix_contact_endpoint_user_type", "contact_endpoints", ["project_id", "user_id", "endpoint_type", "status"])
    _create_index("ix_contact_endpoint_primary", "contact_endpoints", ["project_id", "user_id", "endpoint_type", "is_primary"])
    # A partially applied installation may already contain more than one
    # primary. Keep the most recently seen endpoint deterministically before
    # enforcing the invariant at the database boundary.
    if _table_exists("contact_endpoints"):
        op.execute(sa.text("""
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY project_id, user_id, endpoint_type
                           ORDER BY last_seen_at DESC NULLS LAST, id DESC
                       ) AS position
                FROM contact_endpoints
                WHERE is_primary = true
            )
            UPDATE contact_endpoints AS endpoint
            SET is_primary = false
            FROM ranked
            WHERE endpoint.id = ranked.id AND ranked.position > 1
        """))
    _create_index(
        "uq_contact_endpoint_one_primary",
        "contact_endpoints",
        ["project_id", "user_id", "endpoint_type"],
        unique=True,
        postgresql_where=sa.text("is_primary = true"),
    )

    _create_table(
        "contact_permission_evidence",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sa.BigInteger(), nullable=True),
        sa.Column("channel", sa.String(50), nullable=False),
        sa.Column("permission_type", sa.String(50), server_default="marketing", nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("policy_version", sa.String(100), nullable=True),
        sa.Column("evidence_ref", sa.String(255), nullable=True),
        sa.Column("captured_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["contact_endpoints.id"], ondelete="SET NULL"),
    )
    _create_index("ix_contact_permission_project_id", "contact_permission_evidence", ["project_id"])
    _create_index("ix_contact_permission_user_id", "contact_permission_evidence", ["user_id"])
    _create_index("ix_contact_permission_endpoint", "contact_permission_evidence", ["endpoint_id"])
    _create_index("ix_contact_permission_channel", "contact_permission_evidence", ["channel"])
    _create_index("ix_contact_permission_status", "contact_permission_evidence", ["status"])
    _create_index("ix_contact_permission_evidence_ref", "contact_permission_evidence", ["evidence_ref"])
    _create_index("ix_contact_permission_captured_at", "contact_permission_evidence", ["captured_at"])
    _create_index("ix_contact_permission_expires_at", "contact_permission_evidence", ["expires_at"])
    _create_index(
        "ix_contact_permission_user_channel",
        "contact_permission_evidence",
        ["project_id", "user_id", "channel", "permission_type", "captured_at"],
    )

    _create_table(
        "contact_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("group_type", sa.String(30), server_default="dynamic", nullable=False),
        sa.Column("rule_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_ref", sa.String(255), nullable=True),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "name", name="uq_contact_group_project_name"),
    )
    _create_index("ix_contact_groups_project_id", "contact_groups", ["project_id"])
    _create_index("ix_contact_group_project_status", "contact_groups", ["project_id", "status"])

    _create_table(
        "contact_group_memberships",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(30), server_default="included", nullable=False),
        sa.Column("source", sa.String(50), nullable=True),
        sa.Column("reason", sa.String(100), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["contact_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "group_id", "user_id", name="uq_contact_group_member"),
    )
    _create_index("ix_contact_group_memberships_project_id", "contact_group_memberships", ["project_id"])
    _create_index("ix_contact_group_memberships_group_id", "contact_group_memberships", ["group_id"])
    _create_index("ix_contact_group_memberships_user_id", "contact_group_memberships", ["user_id"])
    _create_index("ix_contact_group_membership_state", "contact_group_memberships", ["project_id", "group_id", "state"])

    _create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("campaign_type", sa.String(30), server_default="one_off", nullable=False),
        sa.Column("status", sa.String(30), server_default="draft", nullable=False),
        sa.Column("default_channel", sa.String(50), server_default="email", nullable=False),
        sa.Column("selection_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("policy_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recurrence_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("target_at", sa.DateTime(), nullable=True),
        sa.Column("external_key", sa.String(255), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "name", name="uq_campaign_project_name"),
        sa.UniqueConstraint("project_id", "external_key", name="uq_campaign_project_external"),
    )
    _create_index("ix_campaigns_project_id", "campaigns", ["project_id"])
    _create_index("ix_campaign_project_status", "campaigns", ["project_id", "status"])

    _create_table(
        "campaign_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("channel", sa.String(50), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
    )
    _create_index("ix_campaign_actions_project_id", "campaign_actions", ["project_id"])
    _create_index("ix_campaign_actions_campaign_id", "campaign_actions", ["campaign_id"])
    _create_index("ix_campaign_action_order", "campaign_actions", ["campaign_id", "position"])

    _create_table(
        "campaign_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("variant_key", sa.String(100), nullable=False),
        sa.Column("locale", sa.String(20), nullable=True),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("variant_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("weight", sa.Integer(), server_default="100", nullable=False),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["action_id"], ["campaign_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["messaging_templates.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("campaign_id", "action_id", "variant_key", "locale", name="uq_campaign_variant_key"),
    )
    _create_index("ix_campaign_variants_project_id", "campaign_variants", ["project_id"])
    _create_index("ix_campaign_variants_campaign_id", "campaign_variants", ["campaign_id"])
    _create_index("ix_campaign_variants_action_id", "campaign_variants", ["action_id"])
    _create_index("ix_campaign_variants_template_id", "campaign_variants", ["template_id"])
    _create_index("ix_campaign_variant_locale", "campaign_variants", ["project_id", "campaign_id", "locale", "status"])

    _create_table(
        "campaign_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("run_key", sa.String(255), nullable=False),
        sa.Column("trigger_type", sa.String(50), server_default="manual", nullable=False),
        sa.Column("status", sa.String(30), server_default="draft", nullable=False),
        sa.Column("audience_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("audience_hash", sa.String(64), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("target_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("capacity_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("candidate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("eligible_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("planned_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("queued_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delivered_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("canceled_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "campaign_id", "run_key", name="uq_campaign_run_key"),
    )
    _create_index("ix_campaign_runs_project_id", "campaign_runs", ["project_id"])
    _create_index("ix_campaign_runs_campaign_id", "campaign_runs", ["campaign_id"])
    _create_index("ix_campaign_runs_audience_hash", "campaign_runs", ["audience_hash"])
    _create_index("ix_campaign_run_due", "campaign_runs", ["project_id", "status", "starts_at"])
    _create_index("ix_campaign_runs_created", "campaign_runs", ["project_id", "created_at"])

    _create_table(
        "campaign_waves",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(30), server_default="planned", nullable=False),
        sa.Column("approval_mode", sa.String(30), server_default="none", nullable=False),
        sa.Column("is_canary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=True),
        sa.Column("window_end", sa.DateTime(), nullable=True),
        sa.Column("planned_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("queued_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["campaign_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("run_id", "position", name="uq_campaign_wave_position"),
        allow_owned_partial=True,
    )
    # Existing installations may already have campaign_waves from an earlier
    # partial run. Reconcile its full additive shape before constraints and
    # indexes, so retrying a previously interrupted rollout is safe.
    if _table_exists("campaign_waves"):
        wave_columns = (
            ("approval_mode", sa.String(30), "none"),
            ("is_canary", sa.Boolean(), sa.text("false")),
            ("approved_at", sa.DateTime(), None),
            ("approved_by_user_id", sa.Integer(), None),
            ("scheduled_at", sa.DateTime(), None),
            ("window_start", sa.DateTime(), None),
            ("window_end", sa.DateTime(), None),
            ("planned_count", sa.Integer(), "0"),
            ("queued_count", sa.Integer(), "0"),
            ("sent_count", sa.Integer(), "0"),
            ("failed_count", sa.Integer(), "0"),
            ("skipped_count", sa.Integer(), "0"),
            ("error_message", sa.Text(), None),
            ("created_at", sa.DateTime(), sa.func.now()),
            ("started_at", sa.DateTime(), None),
            ("finished_at", sa.DateTime(), None),
        )
        for column, column_type, default in wave_columns:
            if not _column_exists("campaign_waves", column):
                kwargs = {"nullable": False} if column in {
                    "approval_mode",
                    "is_canary",
                    "planned_count",
                    "queued_count",
                    "sent_count",
                    "failed_count",
                    "skipped_count",
                    "created_at",
                } else {"nullable": True}
                if default is not None:
                    kwargs["server_default"] = default
                op.add_column("campaign_waves", sa.Column(column, column_type, **kwargs))
        if not _foreign_key_exists("campaign_waves", {"project_id"}, "projects"):
            op.create_foreign_key(
                "fk_campaign_waves_project",
                "campaign_waves",
                "projects",
                ["project_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if not _foreign_key_exists("campaign_waves", {"run_id"}, "campaign_runs"):
            op.create_foreign_key(
                "fk_campaign_waves_run",
                "campaign_waves",
                "campaign_runs",
                ["run_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if not _foreign_key_exists("campaign_waves", {"approved_by_user_id"}, "users"):
            op.create_foreign_key(
                "fk_campaign_waves_approved_by",
                "campaign_waves",
                "users",
                ["approved_by_user_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if not _constraint_exists("campaign_waves", "uq_campaign_wave_position"):
            op.create_unique_constraint(
                "uq_campaign_wave_position",
                "campaign_waves",
                ["run_id", "position"],
            )
        required_wave_columns = {
            "id", "project_id", "run_id", "position", "status",
            *(column for column, _type, _default in wave_columns),
        }
        actual_wave_columns = {
            item["name"]
            for item in inspect(op.get_bind()).get_columns("campaign_waves")
        }
        missing_wave_columns = sorted(required_wave_columns - actual_wave_columns)
        if missing_wave_columns:
            raise RuntimeError(
                "Revision 138 owns campaign_waves, but required columns are "
                f"missing: {', '.join(missing_wave_columns)}"
            )

    _create_index("ix_campaign_waves_project_id", "campaign_waves", ["project_id"])
    _create_index("ix_campaign_waves_run_id", "campaign_waves", ["run_id"])
    _create_index("ix_campaign_waves_approved_by", "campaign_waves", ["approved_by_user_id"])
    _create_index("ix_campaign_wave_due", "campaign_waves", ["project_id", "status", "scheduled_at"])

    _create_table(
        "channel_sender_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("identity_key", sa.String(255), nullable=False),
        sa.Column("address", sa.String(500), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("reply_to", sa.String(500), nullable=True),
        sa.Column("external_instance_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "channel", "identity_key", name="uq_sender_identity_key"),
    )
    _create_index("ix_channel_sender_identities_project_id", "channel_sender_identities", ["project_id"])
    _create_index("ix_channel_sender_identities_channel", "channel_sender_identities", ["channel"])
    _create_index("ix_channel_sender_identities_provider", "channel_sender_identities", ["provider"])
    _create_index("ix_sender_identity_active", "channel_sender_identities", ["project_id", "channel", "status", "is_default"])
    if _table_exists("channel_sender_identities"):
        op.execute(sa.text("""
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY project_id, channel
                           ORDER BY updated_at DESC NULLS LAST, id DESC
                       ) AS position
                FROM channel_sender_identities
                WHERE is_default = true
            )
            UPDATE channel_sender_identities AS identity
            SET is_default = false
            FROM ranked
            WHERE identity.id = ranked.id AND ranked.position > 1
        """))
    _create_index(
        "uq_sender_identity_one_default",
        "channel_sender_identities",
        ["project_id", "channel"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )

    _create_table(
        "channel_delivery_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sender_identity_id", sa.Integer(), nullable=True),
        sa.Column("max_per_minute", sa.Integer(), nullable=True),
        sa.Column("max_per_hour", sa.Integer(), nullable=True),
        sa.Column("max_per_day", sa.Integer(), nullable=True),
        sa.Column("concurrency_limit", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("weight", sa.Integer(), server_default="100", nullable=False),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("warmup_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("health_status", sa.String(30), server_default="unknown", nullable=False),
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_identity_id"], ["channel_sender_identities.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "channel", "provider", "name", name="uq_delivery_profile_name"),
    )
    _create_index("ix_channel_delivery_profiles_project_id", "channel_delivery_profiles", ["project_id"])
    _create_index("ix_channel_delivery_profiles_channel", "channel_delivery_profiles", ["channel"])
    _create_index("ix_channel_delivery_profiles_provider", "channel_delivery_profiles", ["provider"])
    _create_index("ix_channel_delivery_profiles_sender", "channel_delivery_profiles", ["sender_identity_id"])
    _create_index("ix_delivery_profile_select", "channel_delivery_profiles", ["project_id", "channel", "status", "health_status", "priority"])

    _create_table(
        "campaign_recipients",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("wave_id", sa.BigInteger(), nullable=True),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("variant_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("endpoint_id", sa.BigInteger(), nullable=True),
        sa.Column("channel", sa.String(50), server_default="email", nullable=False),
        sa.Column("endpoint_hash", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending", nullable=False),
        sa.Column("suppression_reason", sa.String(120), nullable=True),
        sa.Column("provider", sa.String(80), nullable=True),
        sa.Column("delivery_profile_id", sa.Integer(), nullable=True),
        sa.Column("sender_identity_id", sa.Integer(), nullable=True),
        sa.Column("send_log_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("queued_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["campaign_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["wave_id"], ["campaign_waves.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["action_id"], ["campaign_actions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["variant_id"], ["campaign_variants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["contact_endpoints.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["delivery_profile_id"], ["channel_delivery_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sender_identity_id"], ["channel_sender_identities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["send_log_id"], ["send_logs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_campaign_recipient_idem"),
    )
    _create_index("ix_campaign_recipients_project_id", "campaign_recipients", ["project_id"])
    _create_index("ix_campaign_recipients_run_id", "campaign_recipients", ["run_id"])
    _create_index("ix_campaign_recipients_wave_id", "campaign_recipients", ["wave_id"])
    _create_index("ix_campaign_recipients_action_id", "campaign_recipients", ["action_id"])
    _create_index("ix_campaign_recipients_variant_id", "campaign_recipients", ["variant_id"])
    _create_index("ix_campaign_recipients_user_id", "campaign_recipients", ["user_id"])
    _create_index("ix_campaign_recipients_endpoint", "campaign_recipients", ["endpoint_id"])
    _create_index("ix_campaign_recipients_channel", "campaign_recipients", ["channel"])
    _create_index("ix_campaign_recipients_endpoint_hash", "campaign_recipients", ["endpoint_hash"])
    _create_index("ix_campaign_recipients_profile", "campaign_recipients", ["delivery_profile_id"])
    _create_index("ix_campaign_recipients_sender", "campaign_recipients", ["sender_identity_id"])
    _create_index("ix_campaign_recipients_send_log", "campaign_recipients", ["send_log_id"])
    _create_index("ix_campaign_recipient_dispatch", "campaign_recipients", ["project_id", "status", "scheduled_at"])

    _create_table(
        "channel_capacity_reservations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("delivery_profile_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("wave_id", sa.BigInteger(), nullable=True),
        sa.Column("bucket_start", sa.DateTime(), nullable=False),
        sa.Column("bucket_end", sa.DateTime(), nullable=False),
        sa.Column("units_reserved", sa.Integer(), server_default="0", nullable=False),
        sa.Column("units_consumed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(30), server_default="reserved", nullable=False),
        sa.Column("reservation_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["delivery_profile_id"], ["channel_delivery_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["campaign_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["wave_id"], ["campaign_waves.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("delivery_profile_id", "bucket_start", "reservation_key", name="uq_capacity_reservation_key"),
    )
    _create_index("ix_channel_capacity_reservations_project_id", "channel_capacity_reservations", ["project_id"])
    _create_index("ix_channel_capacity_reservations_profile", "channel_capacity_reservations", ["delivery_profile_id"])
    _create_index("ix_channel_capacity_reservations_run", "channel_capacity_reservations", ["run_id"])
    _create_index("ix_channel_capacity_reservations_wave", "channel_capacity_reservations", ["wave_id"])
    _create_index("ix_channel_capacity_reservations_bucket", "channel_capacity_reservations", ["bucket_start"])
    _create_index("ix_channel_capacity_reservations_status", "channel_capacity_reservations", ["status"])
    _create_index("ix_capacity_reservation_bucket", "channel_capacity_reservations", ["project_id", "delivery_profile_id", "bucket_start", "status"])

    _create_table(
        "contact_verification_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("endpoint_id", sa.BigInteger(), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("verification_type", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("trigger_type", sa.String(50), server_default="manual", nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_status", sa.String(100), nullable=True),
        sa.Column("canonical_status", sa.String(30), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("request_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requested_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["messaging_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["contact_endpoints.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["contact_verification_jobs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_verify_request_idem"),
    )
    _create_index("ix_contact_verification_requests_project_id", "contact_verification_requests", ["project_id"])
    _create_index("ix_contact_verification_requests_user_id", "contact_verification_requests", ["user_id"])
    _create_index("ix_contact_verification_requests_endpoint", "contact_verification_requests", ["endpoint_id"])
    _create_index("ix_contact_verification_requests_job", "contact_verification_requests", ["job_id"])
    _create_index("ix_contact_verification_requests_type", "contact_verification_requests", ["verification_type"])
    _create_index("ix_contact_verification_requests_status", "contact_verification_requests", ["status"])
    _create_index("ix_verify_request_due", "contact_verification_requests", ["project_id", "status", "requested_at"])

    _create_table(
        "operational_alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("alert_type", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(20), server_default="warning", nullable=False),
        sa.Column("status", sa.String(30), server_default="open", nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by_user_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acknowledged_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("workspace_id", "dedupe_key", name="uq_operational_alert_dedupe"),
    )
    _create_index("ix_operational_alerts_workspace_id", "operational_alerts", ["workspace_id"])
    _create_index("ix_operational_alerts_project_id", "operational_alerts", ["project_id"])
    _create_index("ix_operational_alerts_type", "operational_alerts", ["alert_type"])
    _create_index("ix_operational_alerts_severity", "operational_alerts", ["severity"])
    _create_index("ix_operational_alerts_status", "operational_alerts", ["status"])
    _create_index("ix_operational_alert_open", "operational_alerts", ["workspace_id", "status", "severity", "last_seen_at"])

    # Link verification rows to canonical endpoints.  Existing production
    # rows are retained; only a nullable FK is added, so old workers remain
    # compatible while new workers can address an endpoint directly.
    for table in (
        "contact_verification_states",
        "contact_verification_jobs",
        "contact_verification_items",
        "contact_verification_operations",
    ):
        if _table_exists(table) and _add_owned_column(
            table,
            sa.Column("endpoint_id", sa.BigInteger(), nullable=True),
        ):
            constraint_name = f"fk_{table[:20]}_endpoint"
            op.create_foreign_key(
                constraint_name,
                table,
                "contact_endpoints",
                ["endpoint_id"],
                ["id"],
                ondelete="SET NULL",
            )
            _mark_owned("constraint", table, constraint_name)
        _create_index(f"ix_{table[:24]}_endpoint", table, ["endpoint_id"])

    # Project settings gained explicit first-seen/recheck switches.  Keep the
    # legacy *_auto_verify columns intact and only carry a legacy true value
    # into the equivalent recheck switch; enabling first-seen verification is
    # an explicit product decision and is never inferred during migration.
    settings_columns = (
        "email_verify_on_first_seen",
        "email_recheck_enabled",
        "whatsapp_verify_on_first_seen",
        "whatsapp_recheck_enabled",
    )
    if _table_exists("project_verification_settings"):
        for column in settings_columns:
            _add_owned_column(
                "project_verification_settings",
                sa.Column(
                    column,
                    sa.Boolean(),
                    server_default=sa.text("false"),
                    nullable=False,
                ),
            )
        if _column_exists("project_verification_settings", "email_auto_verify"):
            op.execute(sa.text("""
                UPDATE project_verification_settings
                SET email_recheck_enabled = true
                WHERE email_recheck_enabled = false AND email_auto_verify = true
            """))
        if _column_exists("project_verification_settings", "whatsapp_auto_verify"):
            op.execute(sa.text("""
                UPDATE project_verification_settings
                SET whatsapp_recheck_enabled = true
                WHERE whatsapp_recheck_enabled = false AND whatsapp_auto_verify = true
            """))

    # Replace the original per-user/type uniqueness with endpoint-aware partial
    # unique indexes.  Hash-only legacy rows remain unique until reconciled.
    if _table_exists("contact_verification_states"):
        if _constraint_exists("contact_verification_states", "uq_contact_verify_state"):
            op.drop_constraint("uq_contact_verify_state", "contact_verification_states", type_="unique")
        _create_index(
            "uq_contact_verify_state_ep",
            "contact_verification_states",
            ["project_id", "endpoint_id", "verification_type"],
            unique=True,
            postgresql_where=sa.text("endpoint_id IS NOT NULL"),
        )
        _create_index(
            "uq_contact_verify_state_legacy",
            "contact_verification_states",
            ["project_id", "user_id", "verification_type"],
            unique=True,
            postgresql_where=sa.text("endpoint_id IS NULL"),
        )

    # Best-effort primary endpoint backfill.  The application-owned hashes are
    # reused; no unsalted hash is generated in SQL and rows without an existing
    # hash are intentionally left for the ingestion reconciler.
    if _table_exists("messaging_users"):
        op.execute(sa.text("""
            INSERT INTO contact_endpoints (
                project_id, user_id, endpoint_type, value, normalized_value,
                value_hash, is_primary, status, source, first_seen_at,
                last_seen_at, created_at, updated_at
            )
            SELECT source.project_id, source.id, 'email', trim(source.email), lower(trim(source.email)),
                   source.email_hash,
                   NOT EXISTS (
                       SELECT 1 FROM contact_endpoints AS existing
                       WHERE existing.project_id = source.project_id
                         AND existing.user_id = source.id
                         AND existing.endpoint_type = 'email'
                         AND existing.is_primary = true
                   ),
                   'active', 'migration',
                   coalesce(source.created_at, now()), coalesce(source.updated_at, now()), now(), now()
            FROM messaging_users AS source
            WHERE source.email IS NOT NULL
              AND trim(source.email) <> ''
              AND source.email_hash IS NOT NULL
            ON CONFLICT (project_id, user_id, endpoint_type, value_hash) DO NOTHING
        """))
        op.execute(sa.text("""
            INSERT INTO contact_endpoints (
                project_id, user_id, endpoint_type, value, normalized_value,
                value_hash, is_primary, status, source, first_seen_at,
                last_seen_at, created_at, updated_at
            )
            SELECT source.project_id, source.id, 'phone', coalesce(source.phone_e164, source.phone),
                   coalesce(source.phone_e164, source.phone), source.phone_hash,
                   NOT EXISTS (
                       SELECT 1 FROM contact_endpoints AS existing
                       WHERE existing.project_id = source.project_id
                         AND existing.user_id = source.id
                         AND existing.endpoint_type = 'phone'
                         AND existing.is_primary = true
                   ),
                   'active', 'migration',
                   coalesce(source.created_at, now()), coalesce(source.updated_at, now()), now(), now()
            FROM messaging_users AS source
            WHERE coalesce(source.phone_e164, source.phone) IS NOT NULL
              AND trim(coalesce(source.phone_e164, source.phone)) <> ''
              AND source.phone_hash IS NOT NULL
            ON CONFLICT (project_id, user_id, endpoint_type, value_hash) DO NOTHING
        """))

    if _table_exists("contact_verification_states"):
        op.execute(sa.text("""
            UPDATE contact_verification_states AS state
            SET endpoint_id = endpoint.id
            FROM contact_endpoints AS endpoint
            WHERE state.endpoint_id IS NULL
              AND endpoint.project_id = state.project_id
              AND endpoint.user_id = state.user_id
              AND endpoint.value_hash = state.identifier_hash
              AND endpoint.endpoint_type = CASE
                    WHEN state.verification_type = 'whatsapp' THEN 'phone'
                    ELSE state.verification_type
                  END
        """))
    if _table_exists("contact_verification_items"):
        op.execute(sa.text("""
            UPDATE contact_verification_items AS item
            SET endpoint_id = endpoint.id
            FROM contact_endpoints AS endpoint
            WHERE item.endpoint_id IS NULL
              AND item.user_id IS NOT NULL
              AND endpoint.project_id = item.project_id
              AND endpoint.user_id = item.user_id
              AND endpoint.value_hash = item.identifier_hash
              AND endpoint.endpoint_type = CASE
                    WHEN item.verification_type = 'whatsapp' THEN 'phone'
                    ELSE item.verification_type
                  END
        """))

    # Enforce project/workspace equality at the database boundary.  These are
    # installed after backfill so a legacy inconsistency blocks the migration
    # visibly instead of being deleted, reassigned, or silently retained.
    for name, table, columns in _PROJECT_PARENT_UNIQUES:
        _ensure_unique_constraint(name, table, columns)
    for name, table, columns, referred_table, referred_columns in _PROJECT_SCOPED_FOREIGN_KEYS:
        _ensure_scoped_foreign_key(
            name,
            table,
            columns,
            referred_table,
            referred_columns,
        )


def downgrade() -> None:
    # Preflight every irreversible condition before touching the schema.  A
    # same-named table without our ownership comment is pre-existing data and
    # must never be adopted or dropped by this revision.
    unowned_tables = [
        table for table in _NEW_TABLES
        if _table_exists(table) and not _is_owned("table", table)
    ]
    if unowned_tables:
        raise RuntimeError(
            "Refusing unsafe revision 138 downgrade; these tables are not "
            f"owned by the migration: {', '.join(sorted(unowned_tables))}"
        )

    if _table_exists("contact_verification_states"):
        duplicate = op.get_bind().execute(sa.text("""
            SELECT 1
            FROM contact_verification_states
            GROUP BY project_id, user_id, verification_type
            HAVING count(*) > 1
            LIMIT 1
        """)).first()
        if duplicate is not None:
            raise RuntimeError(
                "Cannot downgrade revision 138: endpoint-aware verification "
                "states contain duplicate project/user/type rows. Reconcile "
                "them before restoring the revision 137 uniqueness contract."
            )

    # Remove composite tenant guards first.  The original single-column FKs
    # remain in place until their normal table/column downgrade step.
    for name, table, _columns, _referred_table, _referred_columns in reversed(
        _PROJECT_SCOPED_FOREIGN_KEYS
    ):
        if _table_exists(table):
            fk_names = {
                fk.get("name") for fk in inspect(op.get_bind()).get_foreign_keys(table)
            }
            if name in fk_names and _is_owned("constraint", table, name):
                op.drop_constraint(name, table, type_="foreignkey")

    # Remove optional endpoint links before dropping their target table.
    for table in (
        "contact_verification_operations",
        "contact_verification_items",
        "contact_verification_jobs",
        "contact_verification_states",
    ):
        if table == "contact_verification_states" and _table_exists(table):
            for index in ("uq_contact_verify_state_ep", "uq_contact_verify_state_legacy"):
                if _index_exists(table, index) and _is_owned("index", table, index):
                    op.drop_index(index, table_name=table)
        if _table_exists(table) and _column_exists(table, "endpoint_id"):
            constraint_name = f"fk_{table[:20]}_endpoint"
            fk_names = {
                fk.get("name")
                for fk in inspect(op.get_bind()).get_foreign_keys(table)
            }
            if (
                constraint_name in fk_names
                and _is_owned("constraint", table, constraint_name)
            ):
                op.drop_constraint(constraint_name, table, type_="foreignkey")
            endpoint_index = f"ix_{table[:24]}_endpoint"
            if (
                _index_exists(table, endpoint_index)
                and _is_owned("index", table, endpoint_index)
            ):
                op.drop_index(endpoint_index, table_name=table)
            if _is_owned("column", table, "endpoint_id"):
                op.drop_column(table, "endpoint_id")

    if _table_exists("contact_verification_states") and not _constraint_exists(
        "contact_verification_states", "uq_contact_verify_state"
    ):
        op.create_unique_constraint(
            "uq_contact_verify_state",
            "contact_verification_states",
            ["project_id", "user_id", "verification_type"],
        )

    if _table_exists("project_verification_settings"):
        for column in (
            "whatsapp_recheck_enabled",
            "whatsapp_verify_on_first_seen",
            "email_recheck_enabled",
            "email_verify_on_first_seen",
        ):
            if (
                _column_exists("project_verification_settings", column)
                and _is_owned("column", "project_verification_settings", column)
            ):
                op.drop_column("project_verification_settings", column)

    for table in reversed(_NEW_TABLES):
        if _table_exists(table) and _is_owned("table", table):
            op.drop_table(table)

    # Parent tables that predate this revision keep their original primary
    # keys; remove only the redundant composite keys introduced above.
    for name, table, _columns in reversed(_PROJECT_PARENT_UNIQUES):
        if (
            _table_exists(table)
            and _constraint_exists(table, name)
            and _is_owned("constraint", table, name)
        ):
            op.drop_constraint(name, table, type_="unique")
