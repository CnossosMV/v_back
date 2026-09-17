"""Project Import contract and versioned lifecycle models.

Revision ID: 145_project_import
Revises: 144_event_action_lane
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql


revision = "145_project_import"
down_revision = "144_event_action_lane"
branch_labels = None
depends_on = None


LEGACY_DEFINITION = (
    '{"contract_version":"1.0","key":"legacy-v1",'
    '"types":[{"key":"default","label":"Default","when":{"filters":[]},"catch_all":true}],'
    '"stages":{"default":[]},'
    '"age_buckets":[{"key":"first_hours","max_seconds":86400},'
    '{"key":"first_days","max_seconds":604800},'
    '{"key":"first_weeks","max_seconds":2592000},'
    '{"key":"settled","max_seconds":31536000},{"key":"veteran"}]}'
)
LEGACY_CHECKSUM = "55192f588cbfcd48cff7663ade059a9b2e81af9ae37e6e75314111e977dccc4c"


def _tables():
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str):
    return {item["name"] for item in inspect(op.get_bind()).get_columns(table)}


def _constraints(table: str):
    inspector = inspect(op.get_bind())
    names = {item.get("name") for item in inspector.get_unique_constraints(table)}
    names.update(item.get("name") for item in inspector.get_check_constraints(table))
    names.update(item.get("name") for item in inspector.get_foreign_keys(table))
    return {name for name in names if name}


def _indexes(table: str):
    return {item.get("name") for item in inspect(op.get_bind()).get_indexes(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = _tables()

    # Create the import root before LifecycleModel so the optional source link
    # can be declared without a circular CREATE TABLE dependency.
    if "project_imports" not in tables:
        op.create_table(
            "project_imports",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("client_import_id", sa.String(255), nullable=False),
            sa.Column("contract_version", sa.String(20), nullable=False, server_default="1.0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="created"),
            sa.Column("import_mode", sa.String(30), nullable=False, server_default="fill_missing"),
            sa.Column("source_system", sa.String(100), nullable=False),
            sa.Column("source_project", sa.String(255), nullable=True),
            sa.Column("source_version", sa.String(100), nullable=True),
            sa.Column("manifest", postgresql.JSONB(), nullable=True),
            sa.Column("validation_report", postgresql.JSONB(), nullable=True),
            sa.Column("reconciliation_report", postgresql.JSONB(), nullable=True),
            sa.Column("record_counts", postgresql.JSONB(), nullable=True),
            sa.Column("bundle_storage_key", sa.String(500), nullable=True),
            sa.Column("bundle_checksum", sa.String(64), nullable=True),
            sa.Column("bundle_size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("applied_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("uploaded_at", sa.DateTime(), nullable=True),
            sa.Column("validated_at", sa.DateTime(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("project_id", "client_import_id", name="uq_project_import_client_id"),
            sa.CheckConstraint(
                "status IN ('created','uploading','uploaded','validating','ready','blocked','applying','reconciling','completed','failed','canceled')",
                name="ck_project_import_status",
            ),
            sa.CheckConstraint(
                "import_mode IN ('fill_missing','authoritative_fields')",
                name="ck_project_import_mode",
            ),
        )
        op.create_index("ix_project_imports_project_id", "project_imports", ["project_id"])
        op.create_index("ix_project_imports_status", "project_imports", ["status"])

    tables = _tables()
    if "lifecycle_models" not in tables:
        op.create_table(
            "lifecycle_models",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("contract_version", sa.String(20), nullable=False, server_default="1.0"),
            sa.Column("definition", postgresql.JSONB(), nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("validation_report", postgresql.JSONB(), nullable=True),
            sa.Column("source_import_id", sa.String(36), sa.ForeignKey("project_imports.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("approved_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
            sa.Column("activated_at", sa.DateTime(), nullable=True),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "version", name="uq_lifecycle_model_version"),
            sa.CheckConstraint(
                "status IN ('draft','validated','shadow','active','archived')",
                name="ck_lifecycle_model_status",
            ),
        )
        op.create_index("ix_lifecycle_models_project_id", "lifecycle_models", ["project_id"])
        op.create_index("ix_lifecycle_models_status", "lifecycle_models", ["status"])
        op.create_index(
            "uq_lifecycle_one_active", "lifecycle_models", ["project_id"], unique=True,
            postgresql_where=sa.text("status = 'active'"),
        )
        op.create_index(
            "uq_lifecycle_one_shadow", "lifecycle_models", ["project_id"], unique=True,
            postgresql_where=sa.text("status = 'shadow'"),
        )

    _add_column("project_imports", sa.Column("lifecycle_model_id", sa.Integer(), nullable=True))
    if "fk_project_import_lifecycle" not in _constraints("project_imports"):
        op.create_foreign_key(
            "fk_project_import_lifecycle", "project_imports", "lifecycle_models",
            ["lifecycle_model_id"], ["id"], ondelete="SET NULL",
        )

    tables = _tables()
    if "project_import_records" not in tables:
        op.create_table(
            "project_import_records",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("import_id", sa.String(36), sa.ForeignKey("project_imports.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("record_type", sa.String(40), nullable=False),
            sa.Column("external_id", sa.String(255), nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("target_type", sa.String(80), nullable=True),
            sa.Column("target_id", sa.String(100), nullable=True),
            sa.Column("action", sa.String(30), nullable=True),
            sa.Column("error_code", sa.String(80), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("record_metadata", postgresql.JSONB(), nullable=True),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("import_id", "record_type", "external_id", name="uq_project_import_record"),
            sa.CheckConstraint(
                "status IN ('pending','created','updated','unchanged','skipped','blocked','failed')",
                name="ck_project_import_record_status",
            ),
        )
        op.create_index("ix_project_import_records_import_id", "project_import_records", ["import_id"])
        op.create_index("ix_project_import_records_project_id", "project_import_records", ["project_id"])
        op.create_index("ix_project_import_records_status", "project_import_records", ["status"])
        op.create_index(
            "ix_project_import_record_lookup", "project_import_records",
            ["project_id", "record_type", "external_id"],
        )

    if "project_lifecycle_cutovers" not in _tables():
        op.create_table(
            "project_lifecycle_cutovers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("purpose_key", sa.String(120), nullable=False),
            sa.Column("mode", sa.String(20), nullable=False, server_default="legacy"),
            sa.Column("orchestration_epoch", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lifecycle_model_id", sa.Integer(), sa.ForeignKey("lifecycle_models.id", ondelete="SET NULL"), nullable=True),
            sa.Column("previous_mode", sa.String(20), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("changed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "purpose_key", name="uq_project_lifecycle_cutover"),
            sa.CheckConstraint("mode IN ('legacy','shadow','versya')", name="ck_project_cutover_mode"),
            sa.CheckConstraint("orchestration_epoch >= 0", name="ck_project_cutover_epoch"),
        )
        op.create_index("ix_project_lifecycle_cutovers_project_id", "project_lifecycle_cutovers", ["project_id"])

    # Purpose-aware automations remain inert until their corresponding
    # cutover is explicitly set to `versya`. Null preserves legacy behavior.
    for table in ("event_actions", "funnels", "campaigns"):
        _add_column(table, sa.Column("purpose_key", sa.String(120), nullable=True))
        index_name = f"ix_{table}_purpose_key"
        if index_name not in _indexes(table):
            op.create_index(index_name, table, ["purpose_key"])

    # Every existing project receives an active compatibility model. Assigning
    # existing materializations to it changes no current classification.
    op.execute(text("""
        INSERT INTO lifecycle_models
          (project_id, version, name, status, contract_version, definition, checksum, validation_report)
        SELECT p.id, 1, 'Legacy compatibility model', 'active', '1.0',
               CAST(:definition AS jsonb), :checksum,
               jsonb_build_object('valid', true, 'bootstrap', true, 'warnings', jsonb_build_array())
        FROM projects p
        WHERE NOT EXISTS (SELECT 1 FROM lifecycle_models lm WHERE lm.project_id = p.id)
    """).bindparams(definition=LEGACY_DEFINITION, checksum=LEGACY_CHECKSUM))

    # Position/Base versioning. Columns start nullable for safe backfill and
    # become non-null once every project has its compatibility model.
    for table in ("contact_positions", "contact_position_transitions", "base_cell_messages"):
        _add_column(table, sa.Column("lifecycle_model_id", sa.Integer(), nullable=True))
        op.execute(text(f"""
            UPDATE {table} row
            SET lifecycle_model_id = lm.id
            FROM lifecycle_models lm
            WHERE lm.project_id = row.project_id AND lm.status = 'active'
              AND row.lifecycle_model_id IS NULL
        """))
        if "fk_" + table + "_lifecycle" not in _constraints(table):
            op.create_foreign_key(
                "fk_" + table + "_lifecycle", table, "lifecycle_models",
                ["lifecycle_model_id"], ["id"], ondelete="CASCADE",
            )
        op.alter_column(table, "lifecycle_model_id", existing_type=sa.Integer(), nullable=False)

    _add_column("contact_positions", sa.Column("provenance", postgresql.JSONB(), nullable=True))
    _add_column("contact_positions", sa.Column("explanation", postgresql.JSONB(), nullable=True))
    _add_column("contact_position_transitions", sa.Column("provenance", postgresql.JSONB(), nullable=True))

    constraints = _constraints("contact_positions")
    if "uq_contact_position_user" in constraints:
        op.drop_constraint("uq_contact_position_user", "contact_positions", type_="unique")
    if "uq_contact_position_model_user" not in _constraints("contact_positions"):
        op.create_unique_constraint(
            "uq_contact_position_model_user", "contact_positions",
            ["project_id", "user_id", "lifecycle_model_id"],
        )
    if "ix_contact_pos_proj_type" in _indexes("contact_positions"):
        op.drop_index("ix_contact_pos_proj_type", table_name="contact_positions")
    op.create_index(
        "ix_contact_pos_proj_type", "contact_positions",
        ["project_id", "lifecycle_model_id", "type", "stage", "age_bucket"],
    )

    # Historical facts carry business time and can be replayed without live
    # side effects. Existing events remain live.
    _add_column("messaging_events", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    _add_column("messaging_events", sa.Column("import_id", sa.String(36), nullable=True))
    _add_column("messaging_events", sa.Column("processing_mode", sa.String(20), nullable=False, server_default="live"))
    op.execute(text("UPDATE messaging_events SET occurred_at = COALESCE(client_ts, created_at) WHERE occurred_at IS NULL"))
    op.alter_column(
        "messaging_events", "occurred_at", existing_type=sa.DateTime(timezone=True),
        nullable=False, server_default=sa.func.now(),
    )
    if "fk_messaging_event_import" not in _constraints("messaging_events"):
        op.create_foreign_key(
            "fk_messaging_event_import", "messaging_events", "project_imports",
            ["import_id"], ["id"], ondelete="SET NULL",
        )
    if "ck_msg_event_processing_mode" not in _constraints("messaging_events"):
        op.create_check_constraint(
            "ck_msg_event_processing_mode", "messaging_events",
            "processing_mode IN ('live','derive_only','audit_only')",
        )
    for name, columns in (
        ("ix_messaging_events_occurred_at", ["occurred_at"]),
        ("ix_messaging_events_import_id", ["import_id"]),
        ("ix_messaging_events_processing_mode", ["processing_mode"]),
    ):
        if name not in _indexes("messaging_events"):
            op.create_index(name, "messaging_events", columns)

    for column in (
        sa.Column("import_id", sa.String(36), nullable=True),
        sa.Column("external_message_id", sa.String(255), nullable=True),
        sa.Column("is_historical", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    ):
        _add_column("send_logs", column)
    if "fk_send_log_import" not in _constraints("send_logs"):
        op.create_foreign_key(
            "fk_send_log_import", "send_logs", "project_imports",
            ["import_id"], ["id"], ondelete="SET NULL",
        )
    for name, columns in (
        ("ix_send_logs_import_id", ["import_id"]),
        ("ix_send_logs_external_message_id", ["external_message_id"]),
    ):
        if name not in _indexes("send_logs"):
            op.create_index(name, "send_logs", columns)

    for column in (
        sa.Column("semantic_kind", sa.String(30), nullable=False, server_default="fact"),
        sa.Column("contract_status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("replacement_event_name", sa.String(255), nullable=True),
    ):
        _add_column("messaging_event_schemas", column)
    if "ck_event_schema_semantic_kind" not in _constraints("messaging_event_schemas"):
        op.create_check_constraint(
            "ck_event_schema_semantic_kind", "messaging_event_schemas",
            "semantic_kind IN ('fact','decision','outcome','system')",
        )
    if "ck_event_schema_contract_status" not in _constraints("messaging_event_schemas"):
        op.create_check_constraint(
            "ck_event_schema_contract_status", "messaging_event_schemas",
            "contract_status IN ('active','legacy','deprecated')",
        )


def downgrade() -> None:
    # This downgrade removes only structures owned by this migration. It does
    # not attempt to infer classifications or replay history.
    for table in ("campaigns", "funnels", "event_actions"):
        if table in _tables() and "purpose_key" in _columns(table):
            op.drop_column(table, "purpose_key")
    for table, names in (
        ("messaging_event_schemas", ["replacement_event_name", "contract_status", "semantic_kind"]),
        ("send_logs", ["is_historical", "external_message_id", "import_id"]),
        ("messaging_events", ["processing_mode", "import_id", "occurred_at"]),
    ):
        if table in _tables():
            for name in names:
                if name in _columns(table):
                    op.drop_column(table, name)

    if "contact_positions" in _tables():
        if "uq_contact_position_model_user" in _constraints("contact_positions"):
            op.drop_constraint("uq_contact_position_model_user", "contact_positions", type_="unique")
        if "uq_contact_position_user" not in _constraints("contact_positions"):
            op.create_unique_constraint("uq_contact_position_user", "contact_positions", ["project_id", "user_id"])

    for table, names in (
        ("base_cell_messages", ["lifecycle_model_id"]),
        ("contact_position_transitions", ["provenance", "lifecycle_model_id"]),
        ("contact_positions", ["explanation", "provenance", "lifecycle_model_id"]),
    ):
        if table in _tables():
            for name in names:
                if name in _columns(table):
                    op.drop_column(table, name)

    if "project_imports" in _tables() and "lifecycle_model_id" in _columns("project_imports"):
        op.drop_column("project_imports", "lifecycle_model_id")
    for table in ("project_lifecycle_cutovers", "project_import_records", "lifecycle_models", "project_imports"):
        if table in _tables():
            op.drop_table(table)
