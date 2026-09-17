from __future__ import annotations

import hashlib
import io
import json
import zipfile
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import ContactPosition, EventAction, Funnel, Project, SendLog, User, Workspace
from app.models.campaigns import Campaign
from app.models.messaging import MessagingEvent, MessagingUser
from app.models.project_import import LifecycleModel, ProjectImportRecord, ProjectImportUploadGrant
from app.dependencies import require_project_import_upload_access
from app.routers.project_imports import upload_project_import_bundle_raw
from app.services.funnel_service import FunnelService
from app.services.lifecycle_model_service import (
    LEGACY_DEFINITION,
    LifecycleModelService,
    canonical_checksum,
    lifecycle_contract_document,
    validate_lifecycle_definition,
)
from app.services.orchestration_cutover_service import OrchestrationCutoverService
from app.services.project_import_service import (
    BundleReader,
    ProjectImportError,
    ProjectImportService,
    contract_document,
)


def _definition(field: str = "property.commercial.active_paid") -> dict:
    return {
        "contract_version": "1.0",
        "key": "commercial-v1",
        "types": [
            {
                "key": "customer",
                "when": {"filters": [{"field": field, "operator": "equals", "value": True}]},
            },
            {"key": "unknown", "when": {"filters": []}, "catch_all": True},
        ],
        "stages": {
            "customer": [{"key": "active", "when": {"filters": []}, "catch_all": True}],
            "unknown": [{"key": "unclassified", "when": {"filters": []}, "catch_all": True}],
        },
        "age_buckets": [{"key": "first_days", "max_seconds": 604800}, {"key": "settled"}],
    }


def test_contract_is_customer_push_and_inert():
    contract = contract_document()

    assert contract["contract"] == "versya.project-import"
    assert contract["version"] == "1.0"
    assert contract["identity"]["primary_key"] == "external_id"
    assert "tombstone redirect" in contract["identity"]["external_id_alias_resolution"]
    assert "snapshot wins" in contract["identity"]["alias_snapshot_resolution"]
    assert contract["identity"]["collision_policy"].startswith("block")
    assert contract["history"]["fact_event_processing_mode"] == "derive_only"
    assert "reused" in contract["history"]["cross_import_idempotency"]
    assert "checksum" in contract["lifecycle_idempotency"]
    assert "disabled/draft" in contract["automation_policy"]
    assert contract["transport"]["agent_upload"]["control_plane"] == "MCP"
    assert contract["transport"]["agent_upload"]["data_plane"] == "authenticated HTTPS PUT"
    assert contract["merge_modes"]["authoritative_property_namespace_example"] == "properties.tabloide"
    assert any(step.get("tool") == "audit_project_import" for step in contract["agent_workflow"])
    assert "queued" in contract["states"]
    assert contract["acceptance_gates"]["external_sends"] == 0


def test_bundle_reader_rejects_path_traversal(tmp_path):
    bundle = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("../contacts.ndjson", "{}\n")

    with pytest.raises(ProjectImportError, match="unsafe path"):
        BundleReader(str(bundle)).inspect()


def test_lifecycle_validation_compiles_rules_and_forbids_self_reference():
    assert validate_lifecycle_definition(_definition())["valid"] is True

    unsupported = validate_lifecycle_definition(_definition("property"))
    assert unsupported["valid"] is False
    assert any(item["code"] == "invalid_type_rule" for item in unsupported["errors"])

    recursive = validate_lifecycle_definition(_definition("position.stage"))
    assert recursive["valid"] is False
    assert "cannot depend" in next(
        item["message"] for item in recursive["errors"] if item["code"] == "invalid_type_rule"
    )


def test_lifecycle_contract_separates_platform_semantics_from_tenant_taxonomy():
    contract = lifecycle_contract_document()

    assert contract["canonical"]["platform_axes"] == ["type", "stage", "age"]
    assert contract["canonical"]["entry_clock"] == "type_entered_at"
    assert "not globally canonical" in contract["project_scoped"]["type_keys"]
    assert contract["project_scoped"]["recommended_baseline_only"]["customer"] == [
        "active", "renewal_due", "payment_issue",
    ]
    assert any("authoritative" in question for question in contract["authoring_questions"])


def test_legacy_lifecycle_checksum_matches_migration_bootstrap():
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / "145_project_import.py"
    source = migration_path.read_text(encoding="utf-8")

    assert f'LEGACY_CHECKSUM = "{canonical_checksum(LEGACY_DEFINITION)}"' in source


def test_dynamic_timestamp_wait_contract_is_strict():
    FunnelService.validate_step_config("wait", {
        "wait_type": "until_timestamp",
        "source": "contact",
        "path": "properties.commercial.renewal_at",
        "offset_seconds": -3600,
        "on_missing": "hold",
    })

    with pytest.raises(Exception, match="path is not allowed"):
        FunnelService.validate_step_config("wait", {
            "wait_type": "until_timestamp",
            "source": "contact",
            "path": "password_hash",
        })


def test_legacy_decisions_are_epoch_and_owner_gated():
    service = OrchestrationCutoverService(SimpleNamespace())
    properties = {
        "event_semantic_kind": "decision",
        "purpose_key": "checkout_recovery",
        "orchestration_epoch": 3,
    }

    service.get = lambda *_args, **_kwargs: SimpleNamespace(mode="shadow", orchestration_epoch=3)
    assert service.legacy_decision_block_reason(7, properties) is None

    service.get = lambda *_args, **_kwargs: SimpleNamespace(mode="shadow", orchestration_epoch=4)
    assert service.legacy_decision_block_reason(7, properties)["code"] == "stale_or_future_orchestration_epoch"

    service.get = lambda *_args, **_kwargs: SimpleNamespace(mode="versya", orchestration_epoch=3)
    assert service.legacy_decision_block_reason(7, properties)["code"] == "legacy_decision_after_versya_cutover"

    assert service.legacy_decision_block_reason(7, {**properties, "event_semantic_kind": "fact"}) is None


def test_side_effecting_automations_have_purpose_key():
    assert "purpose_key" in EventAction.__table__.columns
    assert "purpose_key" in Funnel.__table__.columns
    assert "purpose_key" in Campaign.__table__.columns


class _TempStorage:
    def __init__(self, root: Path):
        self.root = root

    def save(self, key: str, data: bytes, _content_type: str) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get_local_path(self, key: str) -> str:
        return str(self.root / key)


def _bundle_bytes() -> bytes:
    timestamp = "2026-08-01T12:00:00Z"
    files: dict[str, bytes] = {
        "contacts.ndjson": (
            json.dumps({
                "external_id": "contact-1",
                "email": "import@example.test",
                "first_seen_at": timestamp,
                "properties": {"commercial": {"active_paid": True}},
            }, separators=(",", ":")) + "\n"
        ).encode(),
        "events.ndjson": (
            json.dumps({
                "external_id": "fact-1",
                "contact_external_id": "contact-1",
                "event_name": "tabloide.fact.signup_completed",
                "occurred_at": timestamp,
                "semantic_kind": "fact",
                "properties": {},
            }, separators=(",", ":")) + "\n" +
            json.dumps({
                "external_id": "decision-1",
                "contact_external_id": "contact-1",
                "event_name": "tabloide.campaign.welcome",
                "occurred_at": timestamp,
                "semantic_kind": "decision",
                "properties": {"purpose_key": "welcome", "orchestration_epoch": 0},
            }, separators=(",", ":")) + "\n"
        ).encode(),
        "messages.ndjson": (
            json.dumps({
                "external_id": "message-1",
                "contact_external_id": "contact-1",
                "occurred_at": timestamp,
                "status": "delivered",
                "channel": "email",
                "recipient": "import@example.test",
            }, separators=(",", ":")) + "\n"
        ).encode(),
        "position_snapshots.ndjson": (
            json.dumps({
                "external_id": "position-1",
                "contact_external_id": "contact-1",
                "type": "customer",
                "stage": "active",
                "type_entered_at": timestamp,
                "position_entered_at": timestamp,
                "stage_entered_at": timestamp,
                "as_of": timestamp,
            }, separators=(",", ":")) + "\n"
        ).encode(),
        "automations/event_schemas.json": json.dumps([{
            "external_id": "signup-schema",
            "event_name": "tabloide.fact.signup_completed",
            "semantic_kind": "fact",
            "contract_status": "active",
        }], separators=(",", ":")).encode(),
        "automations/templates.json": json.dumps([{
            "external_id": "welcome-template",
            "slug": "welcome-imported",
            "name": "Imported welcome",
            "channel_type": "email",
            "body": "Hello",
        }], separators=(",", ":")).encode(),
        "automations/event_actions.json": json.dumps([{
            "external_id": "welcome-action",
            "name": "Imported welcome action",
            "trigger_event": "tabloide.fact.signup_completed",
            "purpose_key": "welcome",
            "actions": [],
        }], separators=(",", ":")).encode(),
        "automations/funnels.json": json.dumps([{
            "external_id": "welcome-funnel",
            "name": "Imported welcome funnel",
            "trigger_type": "event",
            "trigger_config": {"event_name": "tabloide.fact.signup_completed"},
            "purpose_key": "welcome",
            "steps": [{
                "external_id": "wait-1",
                "step_type": "wait",
                "step_config": {"wait_type": "duration", "duration": 1, "unit": "hours"},
            }],
        }], separators=(",", ":")).encode(),
        "automations/campaigns.json": json.dumps([{
            "external_id": "welcome-campaign",
            "name": "Imported welcome campaign",
            "campaign_type": "automation",
            "purpose_key": "welcome",
            "actions": [{"action_type": "send", "channel": "email", "config": {}}],
        }], separators=(",", ":")).encode(),
    }
    descriptors = {}
    for name, data in files.items():
        records = len(json.loads(data)) if name.endswith(".json") else len(data.splitlines())
        descriptors[name] = {"sha256": hashlib.sha256(data).hexdigest(), "records": records}
    manifest = {
        "contract": "versya.project-import",
        "contract_version": "1.0",
        "client_import_id": "integration-import-1",
        "source": {"system": "fixture", "exported_at": timestamp},
        "import_mode": "fill_missing",
        "files": descriptors,
        "lifecycle_model": {
            "external_id": "commercial-v1",
            "name": "Commercial v1",
            "definition": _definition(),
        },
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


def _contacts_bundle_bytes(
    client_import_id: str,
    contacts: list[dict],
    *,
    import_mode: str = "fill_missing",
    authoritative_fields: list[str] | None = None,
    lifecycle_definition: dict | None = None,
    position_snapshots: list[dict] | None = None,
) -> bytes:
    files = {
        "contacts.ndjson": "".join(
            json.dumps(item, separators=(",", ":")) + "\n" for item in contacts
        ).encode(),
    }
    if position_snapshots is not None:
        files["position_snapshots.ndjson"] = "".join(
            json.dumps(item, separators=(",", ":")) + "\n" for item in position_snapshots
        ).encode()
    manifest = {
        "contract": "versya.project-import",
        "contract_version": "1.0",
        "client_import_id": client_import_id,
        "source": {"system": "fixture", "exported_at": "2026-08-01T12:00:00Z"},
        "import_mode": import_mode,
        "files": {
            name: {
                "sha256": hashlib.sha256(data).hexdigest(),
                "records": len(data.splitlines()),
            }
            for name, data in files.items()
        },
    }
    if authoritative_fields is not None:
        manifest["authoritative_fields"] = authoritative_fields
    if lifecycle_definition is not None:
        manifest["lifecycle_model"] = {
            "external_id": "fixture-lifecycle-v1",
            "name": "Fixture lifecycle v1",
            "definition": lifecycle_definition,
        }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


def _history_bundle_bytes(client_import_id: str) -> bytes:
    timestamp = "2026-08-01T12:00:00Z"
    files: dict[str, bytes] = {
        "contacts.ndjson": (
            json.dumps({
                "external_id": "history-contact-1",
                "email": "history@example.test",
            }, separators=(",", ":")) + "\n"
        ).encode(),
        "events.ndjson": (
            json.dumps({
                "external_id": "stable-event-1",
                "contact_external_id": "history-contact-1",
                "event_name": "fixture.fact.created",
                "occurred_at": timestamp,
                "semantic_kind": "fact",
                "properties": {},
            }, separators=(",", ":")) + "\n"
        ).encode(),
        "messages.ndjson": (
            json.dumps({
                "external_id": "stable-message-1",
                "contact_external_id": "history-contact-1",
                "occurred_at": timestamp,
                "status": "delivered",
                "channel": "email",
                "recipient": "history@example.test",
            }, separators=(",", ":")) + "\n"
        ).encode(),
    }
    manifest = {
        "contract": "versya.project-import",
        "contract_version": "1.0",
        "client_import_id": client_import_id,
        "source": {"system": "fixture", "exported_at": timestamp},
        "import_mode": "fill_missing",
        "files": {
            name: {
                "sha256": hashlib.sha256(data).hexdigest(),
                "records": len(data.splitlines()),
            }
            for name, data in files.items()
        },
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


def _project_for_import(db_session, suffix: str) -> tuple[User, Project]:
    owner = User(email=f"owner-{suffix}@example.test", name="Owner")
    db_session.add(owner)
    db_session.flush()
    workspace = Workspace(name=f"Import workspace {suffix}", owner_id=owner.id)
    db_session.add(workspace)
    db_session.flush()
    owner.workspace_id = workspace.id
    project = Project(name=f"Existing project {suffix}", workspace_id=workspace.id)
    db_session.add(project)
    db_session.flush()
    db_session.commit()
    return owner, project


def test_lifecycle_distribution_parity_and_contact_explanation(db_session):
    owner, project = _project_for_import(db_session, "lifecycle-observability")
    paid = MessagingUser(
        project_id=project.id,
        external_id="paid-contact",
        properties={"commercial": {"active_paid": True}},
    )
    unknown = MessagingUser(
        project_id=project.id,
        external_id="unknown-contact",
        properties={"commercial": {"active_paid": False}},
    )
    db_session.add_all([paid, unknown])
    db_session.flush()

    service = LifecycleModelService(db_session)
    model = service.create(
        project.id,
        name="Commercial v1",
        definition=_definition(),
        actor_user_id=owner.id,
        requested_status="validated",
    )
    materialized = service.materialize(model, initial_snapshot=True)
    db_session.commit()

    assert materialized["classified"] == 2
    distribution = service.distribution(model)
    assert distribution["eligible_contacts"] == 2
    assert distribution["positioned_contacts"] == 2
    assert distribution["missing_positions"] == 0
    assert distribution["by_type"] == {"customer": 1, "unknown": 1}

    explanation = service.explain_contact(model, paid)
    assert explanation["matches_materialized"] is True
    assert explanation["current"]["type"] == "customer"
    assert explanation["current"]["type_explanation"]["priority"] == 0
    assert explanation["current"]["type_evidence"][0]["observed"] is True

    paid.properties = {"commercial": {"active_paid": False}}
    db_session.commit()
    comparison = service.compare_materialized(model)
    assert comparison["mismatched"] == 1
    assert comparison["transition_matrix"]["customer/active -> unknown/unclassified"] == 1
    changed = service.explain_contact(model, paid)
    assert changed["matches_materialized"] is False
    assert changed["stored"]["type"] == "customer"
    assert changed["current"]["type"] == "unknown"


def test_validation_warns_for_preexisting_duplicate_when_exact_external_id_owns_email(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "exact-duplicate")
    db_session.add_all([
        MessagingUser(project_id=project.id, external_id="source-contact-1", email="same@example.test"),
        MessagingUser(project_id=project.id, external_id="aud_csv_duplicate", email="same@example.test"),
    ])
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "exact-duplicate-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes("exact-duplicate-import", [{
        "external_id": "source-contact-1",
        "email": "same@example.test",
    }]))

    report = service.validate(row)

    assert report["valid"] is True
    assert not report["errors"]
    assert any(item["code"] == "existing_email_duplicate" for item in report["warnings"])
    assert report["safety"]["auto_merge"] == 0


def test_validation_ignores_merged_tombstone_collision(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "merged-tombstone")
    canonical = MessagingUser(
        project_id=project.id,
        external_id="source-contact",
        email="same@example.test",
    )
    db_session.add(canonical)
    db_session.flush()
    tombstone = MessagingUser(
        project_id=project.id,
        external_id="aud_csv_duplicate",
        email="same@example.test",
        status="merged",
        merged_into=canonical.id,
        created_via="audience_csv_import",
    )
    db_session.add(tombstone)
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "merged-tombstone-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes("merged-tombstone-import", [{
        "external_id": "source-contact",
        "email": "same@example.test",
    }]))

    report = service.validate(row)

    assert report["valid"] is True
    assert not report["errors"]
    assert not any(
        item["code"].startswith("existing_email_")
        for item in report["warnings"]
    )


def test_project_import_applies_contact_through_merged_external_id_alias(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "merged-alias-apply")
    canonical = MessagingUser(
        project_id=project.id,
        external_id="canonical-contact",
        properties={"canonical": True},
    )
    db_session.add(canonical)
    db_session.flush()
    tombstone = MessagingUser(
        project_id=project.id,
        external_id="source-contact",
        status="merged",
        merged_into=canonical.id,
    )
    db_session.add(tombstone)
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "merged-alias-apply-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes("merged-alias-apply-import", [{
        "external_id": "source-contact",
        "properties": {"imported": True},
    }]))
    report = service.validate(row)
    assert report["valid"] is True

    result = service.apply(row, owner.id)

    db_session.refresh(canonical)
    db_session.refresh(tombstone)
    assert result["external_sends"] == 0
    assert canonical.properties == {"imported": True, "canonical": True}
    assert tombstone.status == "merged"
    ledger = db_session.query(ProjectImportRecord).filter_by(
        import_id=row.id,
        record_type="contact",
        external_id="source-contact",
    ).one()
    assert ledger.target_id == str(canonical.id)


def test_authoritative_property_namespace_replaces_only_source_owned_subtree(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "property-namespace")
    contact = MessagingUser(
        project_id=project.id,
        external_id="source-contact",
        properties={
            "tabloide": {"commercial": {"active_paid": True}, "obsolete": True},
            "versya": {"score": 91},
        },
    )
    db_session.add(contact)
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "property-namespace-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "authoritative_fields",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes(
        "property-namespace-import",
        [{
            "external_id": "source-contact",
            "properties": {"tabloide": {"commercial": {"active_paid": False}}},
        }],
        import_mode="authoritative_fields",
        authoritative_fields=["properties.tabloide"],
    ))

    report = service.validate(row)
    assert report["valid"] is True
    result = service.apply(row, owner.id)

    db_session.refresh(contact)
    assert result["external_sends"] == 0
    assert contact.properties == {
        "tabloide": {"commercial": {"active_paid": False}},
        "versya": {"score": 91},
    }


def test_validation_rejects_ambiguous_full_and_namespaced_property_authority(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "ambiguous-property-authority")
    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "ambiguous-property-authority-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "authoritative_fields",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes(
        "ambiguous-property-authority-import",
        [{"external_id": "source-contact", "properties": {"tabloide": {}}}],
        import_mode="authoritative_fields",
        authoritative_fields=["properties", "properties.tabloide"],
    ))

    report = service.validate(row)

    assert report["valid"] is False
    assert any(
        item["code"] == "authoritative_fields" and "not both" in item["message"]
        for item in report["errors"]
    )


def test_validation_blocks_email_collision_for_new_external_id(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "new-collision")
    db_session.add(MessagingUser(
        project_id=project.id,
        external_id="existing-contact",
        email="same@example.test",
    ))
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "new-collision-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes("new-collision-import", [{
        "external_id": "new-source-contact",
        "email": "same@example.test",
    }]))

    report = service.validate(row)

    assert report["valid"] is False
    assert any(item["code"] == "existing_email_collision" for item in report["errors"])


def test_validation_warns_when_fill_missing_will_ignore_colliding_phone(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "ignored-phone")
    db_session.add_all([
        MessagingUser(
            project_id=project.id,
            external_id="source-contact",
            phone="5511000000001",
        ),
        MessagingUser(
            project_id=project.id,
            external_id="other-contact",
            phone="5511000000002",
            phone_e164="5511000000002",
        ),
    ])
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "ignored-phone-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes("ignored-phone-import", [{
        "external_id": "source-contact",
        "phone": "5511000000002",
    }]))

    report = service.validate(row)

    assert report["valid"] is True
    assert not report["errors"]
    assert any(
        item["code"] == "existing_phone_ignored_by_merge_strategy"
        for item in report["warnings"]
    )


def test_validation_blocks_colliding_email_when_exact_contact_field_is_missing(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "missing-email")
    db_session.add_all([
        MessagingUser(project_id=project.id, external_id="source-contact", email=None),
        MessagingUser(project_id=project.id, external_id="other-contact", email="same@example.test"),
    ])
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "missing-email-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes("missing-email-import", [{
        "external_id": "source-contact",
        "email": "same@example.test",
    }]))

    report = service.validate(row)

    assert report["valid"] is False
    assert any(item["code"] == "existing_email_collision" for item in report["errors"])


def test_validation_blocks_authoritative_overwrite_with_colliding_email(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "authoritative-email")
    db_session.add_all([
        MessagingUser(project_id=project.id, external_id="source-contact", email="current@example.test"),
        MessagingUser(project_id=project.id, external_id="other-contact", email="incoming@example.test"),
    ])
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "authoritative-email-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "authoritative_fields",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes(
        "authoritative-email-import",
        [{"external_id": "source-contact", "email": "incoming@example.test"}],
        import_mode="authoritative_fields",
        authoritative_fields=["email"],
    ))

    report = service.validate(row)

    assert report["valid"] is False
    assert any(item["code"] == "existing_email_collision" for item in report["errors"])


def test_project_import_applies_history_inert_and_automations_disabled(db_session, tmp_path):
    owner = User(email="owner@example.test", name="Owner")
    db_session.add(owner)
    db_session.flush()
    workspace = Workspace(name="Import workspace", owner_id=owner.id)
    db_session.add(workspace)
    db_session.flush()
    owner.workspace_id = workspace.id
    project = Project(name="Existing project", workspace_id=workspace.id)
    db_session.add(project)
    db_session.flush()
    legacy = LifecycleModelService(db_session).ensure_legacy_model(project.id, owner.id)
    db_session.commit()

    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    import_row = service.create(project.id, {
        "client_import_id": "integration-import-1",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(import_row, _bundle_bytes())
    report = service.validate(import_row)
    assert report["valid"] is True

    result = service.apply(import_row, owner.id)
    assert result["external_sends"] == 0
    assert result["live_event_dispatch"] == 0

    contact = db_session.query(MessagingUser).filter_by(project_id=project.id, external_id="contact-1").one()
    events = db_session.query(MessagingEvent).filter_by(project_id=project.id, user_id=contact.id).all()
    assert {event.processing_mode for event in events} == {"derive_only", "audit_only"}
    assert all(event.processed for event in events)
    assert db_session.query(SendLog).filter_by(project_id=project.id, is_historical=True).count() == 1
    assert db_session.query(EventAction).filter_by(project_id=project.id, is_active=False, purpose_key="welcome").count() == 1
    assert db_session.query(Funnel).filter_by(project_id=project.id, status="draft", purpose_key="welcome").count() == 1
    assert db_session.query(Campaign).filter_by(project_id=project.id, status="draft", purpose_key="welcome").count() == 1

    imported_model = db_session.query(LifecycleModel).filter_by(source_import_id=import_row.id).one()
    assert legacy.status == "active"
    assert imported_model.status == "validated"
    assert db_session.query(ProjectImportRecord).filter_by(import_id=import_row.id, status="created").count() >= 1

    lifecycle_service = LifecycleModelService(db_session)
    shadow = lifecycle_service.activate(imported_model, target_status="shadow", actor_user_id=owner.id)
    assert shadow["status"] == "shadow"
    comparison = lifecycle_service.compare(legacy, imported_model, sample_limit=100)
    assert comparison["sampled"] == 1
    active = lifecycle_service.activate(imported_model, target_status="active", actor_user_id=owner.id)
    assert active["status"] == "active"
    assert legacy.status == "archived"


def test_agent_preflight_queue_and_audit_are_checksum_bound(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "agent-first")
    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "agent-first-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes(
        "agent-first-import",
        [{"external_id": "agent-contact-1", "email": "agent@example.test"}],
    ))

    first = service.preflight(row)
    second = service.preflight(row)

    assert first["can_apply"] is True
    assert first["bundle_checksum"] == row.bundle_checksum
    assert first["validation_fingerprint"] == second["validation_fingerprint"]
    assert first["gates"]["external_sends"] == 0
    assert first["gates"]["live_event_dispatch"] == 0
    assert first["gates"]["automations_enabled"] == 0

    queued = service.enqueue(row, owner.id)
    assert queued.status == "queued"
    assert queued.apply_requested_at is not None
    assert service.preflight(queued)["can_apply"] is False

    # The production worker atomically claims queued -> applying before using
    # this same idempotent service method.
    queued.status = "applying"
    db_session.commit()
    result = service.apply(queued, owner.id, already_claimed=True)
    audit = service.audit(queued)

    assert result["automations_enabled"] == 0
    assert audit["accepted"] is True
    assert audit["gates"]["failed_or_blocked_ledger_records"] == 0
    assert audit["gates"]["pending_ledger_records_after_completion"] == 0
    records = service.list_records(queued, record_type="contact")
    assert records["page"]["total"] == 1
    assert records["items"][0]["external_id"] == "agent-contact-1"


def test_mcp_upload_grant_is_project_bound_short_lived_and_one_time(db_session, tmp_path, monkeypatch):
    owner, project = _project_for_import(db_session, "upload-grant")
    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "upload-grant-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)

    first, _first_token = service.issue_upload_grant(row, owner.id)
    second, second_token = service.issue_upload_grant(row, owner.id)
    db_session.refresh(first)
    assert first.status == "revoked"
    assert second.status == "active"

    checker = require_project_import_upload_access()
    request = SimpleNamespace(headers={})
    auth = asyncio.run(checker(
        project.id,
        row.id,
        request,
        None,
        second_token,
        db_session,
    ))
    assert auth["auth_kind"] == "project_import_upload_grant"
    assert auth["upload_grant_id"] == second.id

    bundle = _contacts_bundle_bytes(
        "upload-grant-import",
        [{"external_id": "grant-contact-1"}],
    )
    storage = _TempStorage(tmp_path)
    monkeypatch.setattr(
        "app.services.project_import_service.get_storage_backend",
        lambda: storage,
    )

    class UploadRequest:
        headers = {
            "content-type": "application/zip",
            "content-length": str(len(bundle)),
        }

        async def body(self):
            return bundle

    uploaded = asyncio.run(upload_project_import_bundle_raw(
        project.id,
        row.id,
        UploadRequest(),
        auth,
        db_session,
    ))
    assert uploaded.status == "uploaded"
    db_session.refresh(second)
    assert second.status == "used"
    assert second.used_at is not None
    with pytest.raises(Exception, match="invalid or already used"):
        asyncio.run(checker(
            project.id,
            row.id,
            request,
            None,
            second_token,
            db_session,
        ))
    assert db_session.query(ProjectImportUploadGrant).filter_by(
        import_id=row.id,
        status="active",
    ).count() == 0


def test_project_import_reuses_historical_records_across_imports(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "cross-import-history")
    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    rows = []

    for client_import_id in ("history-import-a", "history-import-b"):
        row = service.create(project.id, {
            "client_import_id": client_import_id,
            "source_system": "fixture",
            "contract_version": "1.0",
            "import_mode": "fill_missing",
        }, owner.id)
        service.upload(row, _history_bundle_bytes(client_import_id))
        assert service.validate(row)["valid"] is True
        result = service.apply(row, owner.id)
        assert result["external_sends"] == 0
        assert result["live_event_dispatch"] == 0
        rows.append(row)

    assert db_session.query(MessagingEvent).filter_by(
        project_id=project.id,
        external_event_id="stable-event-1",
    ).count() == 1
    assert db_session.query(SendLog).filter_by(
        project_id=project.id,
        external_message_id="stable-message-1",
    ).count() == 1

    second_records = db_session.query(ProjectImportRecord).filter(
        ProjectImportRecord.import_id == rows[1].id,
        ProjectImportRecord.record_type.in_(("event", "message")),
    ).all()
    assert len(second_records) == 2
    assert {record.status for record in second_records} == {"unchanged"}
    assert {record.action for record in second_records} == {"existing_imported_history"}


def test_project_import_reuses_identical_lifecycle_model_across_imports(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "cross-import-lifecycle")
    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    rows = []

    for client_import_id in ("lifecycle-import-a", "lifecycle-import-b"):
        row = service.create(project.id, {
            "client_import_id": client_import_id,
            "source_system": "fixture",
            "contract_version": "1.0",
            "import_mode": "fill_missing",
        }, owner.id)
        service.upload(row, _contacts_bundle_bytes(
            client_import_id,
            [{"external_id": "lifecycle-contact-1"}],
            lifecycle_definition=_definition(),
        ))
        assert service.validate(row)["valid"] is True
        assert service.apply(row, owner.id)["external_sends"] == 0
        rows.append(row)

    models = db_session.query(LifecycleModel).filter_by(project_id=project.id).all()
    assert len(models) == 1
    assert rows[0].lifecycle_model_id == models[0].id
    assert rows[1].lifecycle_model_id == models[0].id
    second_record = db_session.query(ProjectImportRecord).filter_by(
        import_id=rows[1].id,
        record_type="lifecycle_model",
    ).one()
    assert second_record.status == "unchanged"
    assert second_record.action == "existing_lifecycle_checksum"


def test_project_import_coalesces_alias_snapshots_to_canonical_source(db_session, tmp_path):
    owner, project = _project_for_import(db_session, "alias-snapshot")
    canonical = MessagingUser(project_id=project.id, external_id="canonical-contact")
    db_session.add(canonical)
    db_session.flush()
    tombstone = MessagingUser(
        project_id=project.id,
        external_id="source-alias",
        status="merged",
        merged_into=canonical.id,
    )
    db_session.add(tombstone)
    db_session.commit()

    timestamp = "2026-08-01T12:00:00Z"
    snapshots = [
        {
            "external_id": "canonical-position",
            "contact_external_id": "canonical-contact",
            "type": "customer",
            "stage": "active",
            "type_entered_at": timestamp,
            "position_entered_at": timestamp,
            "stage_entered_at": timestamp,
            "as_of": timestamp,
        },
        {
            "external_id": "alias-position",
            "contact_external_id": "source-alias",
            "type": "prospect",
            "stage": "registered",
            "type_entered_at": timestamp,
            "position_entered_at": timestamp,
            "stage_entered_at": timestamp,
            "as_of": timestamp,
        },
    ]
    service = ProjectImportService(db_session)
    service.storage = _TempStorage(tmp_path)
    row = service.create(project.id, {
        "client_import_id": "alias-snapshot-import",
        "source_system": "fixture",
        "contract_version": "1.0",
        "import_mode": "fill_missing",
    }, owner.id)
    service.upload(row, _contacts_bundle_bytes(
        "alias-snapshot-import",
        [{"external_id": "canonical-contact"}, {"external_id": "source-alias"}],
        lifecycle_definition=_definition(),
        position_snapshots=snapshots,
    ))
    assert service.validate(row)["valid"] is True
    assert service.apply(row, owner.id)["external_sends"] == 0

    position = db_session.query(ContactPosition).filter_by(
        project_id=project.id,
        user_id=canonical.id,
        lifecycle_model_id=row.lifecycle_model_id,
    ).one()
    assert (position.type, position.stage) == ("customer", "active")
    records = db_session.query(ProjectImportRecord).filter(
        ProjectImportRecord.import_id == row.id,
        ProjectImportRecord.record_type == "position_snapshot",
    ).all()
    assert len({record.target_id for record in records}) == 1
    assert {record.action for record in records} == {
        "historical_snapshot",
        "coalesced_alias_snapshot",
    }
