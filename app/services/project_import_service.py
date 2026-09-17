"""Project Import v1 bundle validation and idempotent application."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Iterator

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import BaseCellMessage, EventAction, Funnel, FunnelStep, SendLog
from app.models.campaigns import Campaign, CampaignAction, CampaignVariant
from app.models.messaging import MessagingEvent, MessagingEventSchema, MessagingTemplate, MessagingUser
from app.models.project_import import (
    LifecycleModel,
    ProjectImport,
    ProjectImportRecord,
    ProjectImportUploadGrant,
)
from app.services.lifecycle_model_service import (
    LifecycleModelService,
    canonical_checksum,
    validate_lifecycle_definition,
)
from app.services.storage import get_storage_backend


CONTRACT_VERSION = "1.0"
MAX_BUNDLE_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_FILES = 32
MAX_NDJSON_LINE_BYTES = 2 * 1024 * 1024
RAW_RETENTION_DAYS = 30
UPLOAD_GRANT_MINUTES = 30

REQUIRED_FILES = {"manifest.json", "contacts.ndjson"}
ALLOWED_FILES = REQUIRED_FILES | {
    "events.ndjson",
    "permissions.ndjson",
    "messages.ndjson",
    "position_snapshots.ndjson",
    "position_transitions.ndjson",
    "automations/templates.json",
    "automations/event_schemas.json",
    "automations/event_actions.json",
    "automations/funnels.json",
    "automations/campaigns.json",
}
TERMINAL_MESSAGE_STATUSES = {
    "sent", "delivered", "read", "failed", "bounced", "canceled", "cancelled", "superseded",
}
CONTACT_FIELDS = {
    "email", "phone", "name", "locale", "timezone", "lifecycle_stage", "segment_name",
    "first_seen_at", "last_seen_at", "is_subscribed", "consent_marketing", "consent_analytics",
    "consent_given_at", "consent_version", "global_opt_out", "opted_out_channels", "tags", "properties",
}
AUTHORITATIVE_PROPERTY_NAMESPACE_RE = re.compile(
    r"^properties\.([A-Za-z0-9][A-Za-z0-9_-]{0,63})$"
)


class ProjectImportError(ValueError):
    pass


def contract_document() -> dict[str, Any]:
    return {
        "contract": "versya.project-import",
        "version": CONTRACT_VERSION,
        "transport": {
            "content_type": "application/zip",
            "max_bundle_bytes": MAX_BUNDLE_BYTES,
            "max_uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
            "raw_retention_days": RAW_RETENTION_DAYS,
            "agent_upload": {
                "control_plane": "MCP",
                "data_plane": "authenticated HTTPS PUT",
                "request_content_type": "application/zip",
                "authentication": "use the one-time X-Versya-Upload-Token returned by create_project_import",
                "endpoint_source": "create_project_import.upload.raw_url",
                "reason": "binary bundles are not base64-encoded into model context",
            },
        },
        "required_files": sorted(REQUIRED_FILES),
        "optional_files": sorted(ALLOWED_FILES - REQUIRED_FILES),
        "identity": {
            "primary_key": "external_id",
            "email_phone_matching": "collision_check_only",
            "external_id_alias_resolution": "A merged external_id follows its tombstone redirect to the active non-sandbox canonical contact; tombstones never own collisions or receive imported data.",
            "alias_snapshot_resolution": "When multiple source external_ids resolve to one canonical contact, its own external_id snapshot wins and the other snapshot ledger rows are coalesced.",
            "collision_policy": "block ambiguous writes; warn when the merge strategy leaves the target unchanged or the exact external_id already owns the value; never auto-merge",
        },
        "merge_modes": {
            "fill_missing": "Existing non-empty profile fields win; restrictive opt-out still wins.",
            "authoritative_fields": (
                "Only manifest.authoritative_fields may overwrite profile fields. Use properties to replace "
                "the complete property object, or properties.<namespace> to replace exactly one top-level "
                "source-owned namespace while retaining every other namespace."
            ),
            "authoritative_property_namespace_example": "properties.tabloide",
        },
        "history": {
            "fact_event_processing_mode": "derive_only",
            "decision_event_processing_mode": "audit_only",
            "messages": "terminal ledger only; never candidate",
            "cross_import_idempotency": "Stable event and message external_ids already imported into the project are reused, never inserted again.",
            "default_window_days": 365,
        },
        "lifecycle_idempotency": "An identical lifecycle definition checksum reuses the existing project model instead of creating a redundant version.",
        "automation_policy": "Imported templates, Event Actions, funnels and campaigns are disabled/draft.",
        "manifest_required": [
            "contract", "contract_version", "client_import_id", "source", "files", "import_mode",
        ],
        "record_identity": {
            "contacts.ndjson": "external_id",
            "events.ndjson": "external_id (source event id) + contact_external_id",
            "messages.ndjson": "external_id (source message id) + contact_external_id",
        },
        "record_requirements": {
            "contacts.ndjson": {
                "required": ["external_id"],
                "profile_fields": sorted(CONTACT_FIELDS),
            },
            "events.ndjson": {
                "required": ["external_id", "contact_external_id", "event_name", "occurred_at"],
                "semantic_kind": ["fact", "decision", "outcome", "system"],
            },
            "permissions.ndjson": {
                "required": ["external_id", "contact_external_id"],
                "rule": "permission evidence is historical; restrictive opt-out wins",
            },
            "messages.ndjson": {
                "required": ["external_id", "contact_external_id", "occurred_at", "status"],
                "allowed_statuses": sorted(TERMINAL_MESSAGE_STATUSES),
            },
            "position_snapshots.ndjson": {
                "required": ["external_id", "contact_external_id", "type", "as_of"],
            },
            "position_transitions.ndjson": {
                "required": ["external_id", "contact_external_id", "occurred_at"],
            },
        },
        "agent_workflow": [
            {"step": 1, "tool": "list_projects", "result": "resolve and retain an explicit project_id"},
            {"step": 2, "tool": "get_project_import_contract", "result": "generate a compatible deterministic bundle"},
            {"step": 3, "tool": "create_project_import", "result": "create or resume by stable client_import_id and receive raw_url"},
            {"step": 4, "action": "PUT the ZIP bytes to upload.raw_url with the returned headers, including the one-time upload token"},
            {"step": 5, "tool": "validate_project_import", "result": "fix every blocking finding and retain warnings"},
            {"step": 6, "tool": "apply_project_import", "arguments": {"dry_run": True}, "result": "revalidate and inspect checksum-bound safety gates"},
            {"step": 7, "tool": "apply_project_import", "arguments": {"dry_run": False}, "result": "request confirmation; repeat with confirm_token to enqueue"},
            {"step": 8, "tool": "get_project_import", "result": "poll queued/applying/reconciling until terminal"},
            {"step": 9, "tool": "audit_project_import", "result": "require every acceptance gate to pass"},
            {"step": 10, "action": "review lifecycle in shadow and transfer each purpose separately; import completion never activates either"},
        ],
        "acceptance_gates": {
            "validation_valid": True,
            "blocking_findings": 0,
            "failed_or_blocked_ledger_records": 0,
            "pending_ledger_records_after_completion": 0,
            "external_sends": 0,
            "live_event_dispatch": 0,
            "automations_enabled": 0,
        },
        "blocking_finding_actions": {
            "checksum": "regenerate the manifest from the exact exported bytes",
            "unknown_contact": "include the referenced contact or omit the orphan historical record",
            "existing_email_collision": "make an explicit identity decision; never auto-merge",
            "existing_phone_collision": "make an explicit identity decision; never auto-merge",
            "existing_contact_not_active": "repair the identity alias in Versya or use the durable canonical source external_id",
            "bundle_error": "repair the ZIP/JSON structure and upload a corrected bundle",
        },
        "states": [
            "created", "uploading", "uploaded", "validating", "ready", "blocked",
            "queued", "applying", "reconciling", "completed", "failed", "canceled",
        ],
    }


def _canonical_checksum(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validation_fingerprint(report: dict[str, Any] | None) -> str | None:
    """Bind an approval to the exact validation findings and safety gates."""
    if not report:
        return None
    normalized = dict(report)
    for field in ("errors", "warnings"):
        normalized[field] = sorted(
            normalized.get(field) or [],
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    return _canonical_checksum(normalized)


def _parse_datetime(value: Any, field: str, *, required: bool = False) -> datetime | None:
    if value in (None, ""):
        if required:
            raise ProjectImportError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProjectImportError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts and str(path) == name.replace("\\", "/")


class BundleReader:
    def __init__(self, path: str):
        self.path = path

    def inspect(self) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
        archive = zipfile.ZipFile(self.path, "r")
        file_entries = [item for item in archive.infolist() if not item.is_dir()]
        entries = {item.filename: item for item in file_entries}
        if len(entries) != len(file_entries):
            archive.close()
            raise ProjectImportError("bundle contains duplicate file names")
        if len(entries) > MAX_ARCHIVE_FILES:
            archive.close()
            raise ProjectImportError(f"bundle has more than {MAX_ARCHIVE_FILES} files")
        if any(not _safe_archive_name(name) for name in entries):
            archive.close()
            raise ProjectImportError("bundle contains an unsafe path")
        total = sum(item.file_size for item in entries.values())
        if total > MAX_UNCOMPRESSED_BYTES:
            archive.close()
            raise ProjectImportError("bundle exceeds the uncompressed size limit")
        return archive, entries

    @staticmethod
    def json_file(archive: zipfile.ZipFile, name: str) -> Any:
        try:
            return json.loads(archive.read(name).decode("utf-8-sig"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectImportError(f"{name} is not valid UTF-8 JSON") from exc

    @staticmethod
    def ndjson(archive: zipfile.ZipFile, name: str) -> Iterator[tuple[int, dict[str, Any]]]:
        try:
            handle = archive.open(name, "r")
        except KeyError:
            return
        with handle:
            for line_no, raw in enumerate(handle, 1):
                if len(raw) > MAX_NDJSON_LINE_BYTES:
                    raise ProjectImportError(f"{name}:{line_no} exceeds the line-size limit")
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProjectImportError(f"{name}:{line_no} is not valid UTF-8 JSON") from exc
                if not isinstance(value, dict):
                    raise ProjectImportError(f"{name}:{line_no} must be a JSON object")
                yield line_no, value


class ProjectImportService:
    def __init__(self, db: Session):
        self.db = db
        self.storage = get_storage_backend()

    @staticmethod
    def _canonical_contact_maps(
        users: list[MessagingUser],
    ) -> tuple[
        dict[str, MessagingUser],
        list[MessagingUser],
        dict[str, MessagingUser],
    ]:
        """Resolve every durable external-id alias to one active canonical contact.

        Merged rows are tombstones, not competing identity owners. Imports must
        validate and apply through their ``merged_into`` chain so historical
        source IDs continue to address the canonical contact without reviving
        or writing new data onto a tombstone.
        """
        by_row_id = {user.id: user for user in users}
        by_external_id = {user.external_id: user for user in users}
        aliases: dict[str, MessagingUser] = {}
        active_by_id: dict[int, MessagingUser] = {}
        unresolved: dict[str, MessagingUser] = {}

        for alias in users:
            current = alias
            seen: set[int] = set()
            while current.status == "merged" and current.merged_into is not None:
                if current.id in seen:
                    current = None
                    break
                seen.add(current.id)
                current = by_row_id.get(current.merged_into)
                if current is None:
                    break
            if (
                current is not None
                and current.status == "active"
                and not current.is_sandbox
            ):
                aliases[alias.external_id] = current
                active_by_id[current.id] = current
            else:
                unresolved[alias.external_id] = alias

        # Keep the raw map available to callers that need to distinguish a
        # missing external ID from an existing but unusable tombstone/deletion.
        return aliases, list(active_by_id.values()), {
            external_id: by_external_id[external_id]
            for external_id in unresolved
        }

    def get(self, project_id: int, import_id: str) -> ProjectImport:
        row = self.db.query(ProjectImport).filter(
            ProjectImport.id == import_id,
            ProjectImport.project_id == project_id,
        ).first()
        if not row:
            raise ProjectImportError("Project import not found")
        return row

    def create(self, project_id: int, values: dict[str, Any], actor_user_id: int | None) -> ProjectImport:
        existing = self.db.query(ProjectImport).filter(
            ProjectImport.project_id == project_id,
            ProjectImport.client_import_id == values["client_import_id"],
        ).first()
        if existing:
            if existing.source_system != values["source_system"] or existing.contract_version != values.get("contract_version", CONTRACT_VERSION):
                raise ProjectImportError("client_import_id was already used with different import metadata")
            return existing
        if values.get("contract_version", CONTRACT_VERSION) != CONTRACT_VERSION:
            raise ProjectImportError(f"Unsupported Project Import contract version: {values.get('contract_version')}")
        row = ProjectImport(
            id=str(uuid.uuid4()),
            project_id=project_id,
            created_by_user_id=actor_user_id,
            expires_at=datetime.utcnow() + timedelta(days=RAW_RETENTION_DAYS),
            **values,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def upload(self, row: ProjectImport, content: bytes) -> ProjectImport:
        if row.status not in {"created", "uploading", "uploaded", "blocked", "failed"}:
            raise ProjectImportError(f"Cannot upload a bundle while import is {row.status}")
        if not content or len(content) > MAX_BUNDLE_BYTES:
            raise ProjectImportError("Bundle is empty or exceeds the compressed size limit")
        checksum = hashlib.sha256(content).hexdigest()
        key = f"project-imports/{row.project_id}/{row.id}/{checksum}.zip"
        self.storage.save(key, content, "application/zip")
        row.bundle_storage_key = key
        row.bundle_checksum = checksum
        row.bundle_size_bytes = len(content)
        row.status = "uploaded"
        row.uploaded_at = datetime.utcnow()
        row.validation_report = None
        row.error_message = None
        self.db.commit()
        self.db.refresh(row)
        return row

    def issue_upload_grant(
        self,
        row: ProjectImport,
        user_id: int,
        ttl_minutes: int = UPLOAD_GRANT_MINUTES,
    ) -> tuple[ProjectImportUploadGrant, str]:
        """Issue a one-time capability so an MCP client need not expose OAuth."""
        self.db.query(ProjectImportUploadGrant).filter(
            ProjectImportUploadGrant.import_id == row.id,
            ProjectImportUploadGrant.status == "active",
        ).update({"status": "revoked"}, synchronize_session=False)
        raw_token = secrets.token_urlsafe(32)
        grant = ProjectImportUploadGrant(
            import_id=row.id,
            project_id=row.project_id,
            user_id=user_id,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            status="active",
            expires_at=datetime.utcnow() + timedelta(minutes=max(1, min(ttl_minutes, 120))),
        )
        self.db.add(grant)
        self.db.commit()
        self.db.refresh(grant)
        return grant, raw_token

    def claim_upload_grant(self, grant_id: int) -> None:
        updated = self.db.query(ProjectImportUploadGrant).filter(
            ProjectImportUploadGrant.id == grant_id,
            ProjectImportUploadGrant.status == "active",
        ).update({"status": "claimed"}, synchronize_session=False)
        self.db.commit()
        if updated != 1:
            raise ProjectImportError("Project Import upload grant is already in use")

    def release_upload_grant(self, grant_id: int) -> None:
        self.db.query(ProjectImportUploadGrant).filter(
            ProjectImportUploadGrant.id == grant_id,
            ProjectImportUploadGrant.status == "claimed",
        ).update({"status": "active"}, synchronize_session=False)
        self.db.commit()

    def consume_upload_grant(self, grant_id: int) -> None:
        grant = self.db.query(ProjectImportUploadGrant).filter(
            ProjectImportUploadGrant.id == grant_id,
        ).first()
        if not grant or grant.status != "claimed":
            raise ProjectImportError("Project Import upload grant is not claimed")
        grant.status = "used"
        grant.used_at = datetime.utcnow()
        self.db.commit()

    @staticmethod
    def _manifest_files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        files = manifest.get("files")
        if isinstance(files, list):
            return {str(item.get("path")): item for item in files if isinstance(item, dict) and item.get("path")}
        if isinstance(files, dict):
            return {str(key): value if isinstance(value, dict) else {} for key, value in files.items()}
        return {}

    def validate(self, row: ProjectImport) -> dict[str, Any]:
        if not row.bundle_storage_key:
            raise ProjectImportError("Upload the bundle before validation")
        if row.status in {"applying", "reconciling", "completed", "canceled"}:
            raise ProjectImportError(f"Cannot validate an import while it is {row.status}")
        row.status = "validating"
        self.db.commit()
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        manifest: dict[str, Any] = {}
        try:
            reader = BundleReader(self.storage.get_local_path(row.bundle_storage_key))
            archive, entries = reader.inspect()
            with archive:
                names = set(entries)
                for missing in sorted(REQUIRED_FILES - names):
                    errors.append({"code": "missing_file", "file": missing, "message": "Required file is missing"})
                for unknown in sorted(names - ALLOWED_FILES):
                    errors.append({"code": "unknown_file", "file": unknown, "message": "File is not part of contract v1"})
                if "manifest.json" not in names:
                    raise ProjectImportError("manifest.json is required")
                manifest = reader.json_file(archive, "manifest.json")
                if not isinstance(manifest, dict):
                    raise ProjectImportError("manifest.json must be an object")
                for field in ("contract", "contract_version", "client_import_id", "source", "files", "import_mode"):
                    if field not in manifest:
                        errors.append({"code": "manifest_required", "file": "manifest.json", "message": f"{field} is required"})
                if manifest.get("contract") != "versya.project-import" or manifest.get("contract_version") != CONTRACT_VERSION:
                    errors.append({"code": "contract", "file": "manifest.json", "message": "Unsupported contract or version"})
                if manifest.get("client_import_id") != row.client_import_id:
                    errors.append({"code": "client_import_id", "file": "manifest.json", "message": "client_import_id does not match the created import"})
                source = manifest.get("source") or {}
                if source.get("system") != row.source_system:
                    errors.append({"code": "source_system", "file": "manifest.json", "message": "source.system does not match the created import"})
                if manifest.get("import_mode", row.import_mode) != row.import_mode:
                    errors.append({"code": "import_mode", "file": "manifest.json", "message": "import_mode does not match the created import"})
                authoritative = manifest.get("authoritative_fields") or []
                if row.import_mode == "authoritative_fields":
                    invalid = sorted(
                        field for field in set(authoritative)
                        if field not in CONTACT_FIELDS
                        and not AUTHORITATIVE_PROPERTY_NAMESPACE_RE.fullmatch(str(field))
                    )
                    if invalid:
                        errors.append({"code": "authoritative_fields", "file": "manifest.json", "message": "Unsupported authoritative fields: " + ", ".join(invalid)})
                    property_namespaces = sorted(
                        field for field in set(authoritative)
                        if AUTHORITATIVE_PROPERTY_NAMESPACE_RE.fullmatch(str(field))
                    )
                    if "properties" in authoritative and property_namespaces:
                        errors.append({
                            "code": "authoritative_fields",
                            "file": "manifest.json",
                            "message": (
                                "Use either properties for full replacement or properties.<namespace> "
                                "for scoped replacement, not both"
                            ),
                        })

                descriptors = self._manifest_files(manifest)
                if "contacts.ndjson" not in descriptors:
                    errors.append({"code": "manifest_file", "file": "manifest.json", "message": "files must declare contacts.ndjson"})
                for name in sorted(names - {"manifest.json"}):
                    descriptor = descriptors.get(name)
                    if descriptor is None:
                        errors.append({"code": "manifest_file", "file": name, "message": "File is not declared by manifest.files"})
                        continue
                    actual = hashlib.sha256(archive.read(name)).hexdigest()
                    if descriptor.get("sha256") != actual:
                        errors.append({"code": "checksum", "file": name, "message": "SHA-256 does not match manifest"})
                for declared in sorted(set(descriptors) - (names - {"manifest.json"})):
                    errors.append({"code": "missing_declared_file", "file": declared, "message": "File is declared by manifest but absent from bundle"})

                contact_ids: set[str] = set()
                email_owner: dict[str, str] = {}
                phone_owner: dict[str, str] = {}
                if "contacts.ndjson" in names:
                    for line_no, item in reader.ndjson(archive, "contacts.ndjson"):
                        external_id = str(item.get("external_id") or "").strip()
                        if not external_id:
                            errors.append({"code": "external_id", "file": "contacts.ndjson", "line": line_no, "message": "external_id is required"})
                            continue
                        if external_id in contact_ids:
                            errors.append({"code": "duplicate_external_id", "file": "contacts.ndjson", "line": line_no, "message": external_id})
                        contact_ids.add(external_id)
                        for field in ("first_seen_at", "last_seen_at", "consent_given_at"):
                            try:
                                _parse_datetime(item.get(field), field)
                            except ProjectImportError as exc:
                                errors.append({"code": field, "file": "contacts.ndjson", "line": line_no, "message": str(exc)})
                        for field, owners, normalized in (
                            ("email", email_owner, str(item.get("email") or "").strip().lower()),
                            ("phone", phone_owner, re.sub(r"\D", "", str(item.get("phone") or ""))),
                        ):
                            if not normalized:
                                continue
                            previous = owners.get(normalized)
                            if previous and previous != external_id:
                                errors.append({
                                    "code": f"{field}_collision",
                                    "file": "contacts.ndjson",
                                    "line": line_no,
                                    "source_external_id": external_id,
                                    "conflicting_source_external_id": previous,
                                    "message": f"{field} is shared by multiple source contacts",
                                })
                            owners[normalized] = external_id
                    counts["contacts"] = len(contact_ids)
                    expected = descriptors.get("contacts.ndjson", {}).get("records")
                    if expected is not None and int(expected) != len(contact_ids):
                        errors.append({"code": "record_count", "file": "contacts.ndjson", "message": f"manifest={expected}, actual={len(contact_ids)}"})

                all_existing_users = self.db.query(MessagingUser).filter(
                    MessagingUser.project_id == row.project_id
                ).all()
                existing_by_id, existing_users, unresolved_existing = (
                    self._canonical_contact_maps(all_existing_users)
                )
                for external_id in sorted(contact_ids):
                    if external_id in unresolved_existing:
                        errors.append({
                            "code": "existing_contact_not_active",
                            "file": "contacts.ndjson",
                            "source_external_id": external_id,
                            "message": "The source external_id exists but does not resolve to an active non-sandbox canonical contact.",
                        })
                authoritative_fields = set(authoritative)

                def profile_field_will_write(target: MessagingUser | None, field: str) -> bool:
                    """Mirror _merge_contact so validation blocks only effective writes."""
                    if target is None:
                        return True
                    if row.import_mode == "authoritative_fields" and field in authoritative_fields:
                        return True
                    return getattr(target, field) in (None, "", [])

                def record_existing_collision(
                    item: MessagingUser,
                    field: str,
                    normalized: str,
                    owners: dict[str, str],
                ) -> None:
                    source_external_id = owners.get(normalized)
                    if not normalized or not source_external_id or source_external_id == item.external_id:
                        return
                    exact_target = existing_by_id.get(source_external_id)
                    # The imported external ID may itself be a tombstone alias
                    # of this canonical row. That is one identity, not a
                    # collision, and must not produce a warning or block.
                    if exact_target is not None and exact_target.id == item.id:
                        return
                    if field == "email":
                        exact_value = (
                            str(exact_target.email or "").strip().lower()
                            if exact_target else ""
                        )
                    else:
                        exact_value = (
                            re.sub(r"\D", "", str(exact_target.phone_e164 or exact_target.phone or ""))
                            if exact_target else ""
                        )
                    detail = {
                        "file": "contacts.ndjson",
                        "source_external_id": source_external_id,
                        "conflicting_external_id": item.external_id,
                    }
                    if exact_target is not None and exact_value == normalized:
                        warnings.append({
                            **detail,
                            "code": f"existing_{field}_duplicate",
                            "message": f"Exact external_id already owns this {field}; another pre-existing contact also has it. No automatic merge will occur.",
                        })
                    elif not profile_field_will_write(exact_target, field):
                        warnings.append({
                            **detail,
                            "code": f"existing_{field}_ignored_by_merge_strategy",
                            "message": f"{field.title()} belongs to another existing contact, but the exact external_id already has a {field} and the selected merge strategy will not overwrite it.",
                        })
                    else:
                        errors.append({
                            **detail,
                            "code": f"existing_{field}_collision",
                            "message": f"{field.title()} belongs to another existing contact and the source external_id does not already own it.",
                        })

                for item in existing_users:
                    record_existing_collision(
                        item,
                        "email",
                        str(item.email or "").strip().lower(),
                        email_owner,
                    )
                    record_existing_collision(
                        item,
                        "phone",
                        re.sub(r"\D", "", str(item.phone_e164 or item.phone or "")),
                        phone_owner,
                    )

                known_contacts = contact_ids | set(existing_by_id)
                for filename in ("events.ndjson", "permissions.ndjson", "messages.ndjson", "position_snapshots.ndjson", "position_transitions.ndjson"):
                    if filename not in names:
                        continue
                    seen_records: set[str] = set()
                    record_count = 0
                    for line_no, item in reader.ndjson(archive, filename):
                        record_count += 1
                        external_id = str(item.get("external_id") or "").strip()
                        if not external_id:
                            errors.append({"code": "external_id", "file": filename, "line": line_no, "message": "source record external_id is required"})
                        elif external_id in seen_records:
                            errors.append({"code": "duplicate_external_id", "file": filename, "line": line_no, "message": external_id})
                        seen_records.add(external_id)
                        contact_external_id = str(item.get("contact_external_id") or "").strip()
                        if not contact_external_id or contact_external_id not in known_contacts:
                            errors.append({"code": "unknown_contact", "file": filename, "line": line_no, "message": contact_external_id or "contact_external_id is required"})
                        if filename == "events.ndjson":
                            if not item.get("event_name"):
                                errors.append({"code": "event_name", "file": filename, "line": line_no, "message": "event_name is required"})
                            try:
                                _parse_datetime(item.get("occurred_at"), "occurred_at", required=True)
                            except ProjectImportError as exc:
                                errors.append({"code": "occurred_at", "file": filename, "line": line_no, "message": str(exc)})
                            if str(item.get("semantic_kind") or "fact") not in {"fact", "decision", "outcome", "system"}:
                                errors.append({"code": "semantic_kind", "file": filename, "line": line_no, "message": "semantic_kind must be fact, decision, outcome, or system"})
                        if filename == "messages.ndjson":
                            if str(item.get("status") or "").lower() not in TERMINAL_MESSAGE_STATUSES:
                                errors.append({"code": "non_terminal_message", "file": filename, "line": line_no, "message": "Imported messages must have a terminal status"})
                            try:
                                _parse_datetime(item.get("occurred_at"), "occurred_at", required=True)
                            except ProjectImportError as exc:
                                errors.append({"code": "occurred_at", "file": filename, "line": line_no, "message": str(exc)})
                        if filename == "permissions.ndjson":
                            try:
                                _parse_datetime(item.get("consent_given_at"), "consent_given_at")
                            except ProjectImportError as exc:
                                errors.append({"code": "consent_given_at", "file": filename, "line": line_no, "message": str(exc)})
                        if filename == "position_snapshots.ndjson":
                            if not item.get("type"):
                                errors.append({"code": "position_type", "file": filename, "line": line_no, "message": "type is required"})
                            for field in (
                                "position_entered_at", "type_entered_at", "stage_entered_at", "as_of",
                            ):
                                try:
                                    _parse_datetime(item.get(field), field, required=field == "as_of")
                                except ProjectImportError as exc:
                                    errors.append({"code": field, "file": filename, "line": line_no, "message": str(exc)})
                        if filename == "position_transitions.ndjson":
                            try:
                                _parse_datetime(item.get("occurred_at"), "occurred_at", required=True)
                            except ProjectImportError as exc:
                                errors.append({"code": "occurred_at", "file": filename, "line": line_no, "message": str(exc)})
                    counts[filename.removesuffix(".ndjson")] = record_count
                    expected = descriptors.get(filename, {}).get("records")
                    if expected is not None and int(expected) != record_count:
                        errors.append({"code": "record_count", "file": filename, "message": f"manifest={expected}, actual={record_count}"})

                for filename in sorted(name for name in names if name.endswith(".json") and name != "manifest.json"):
                    value = reader.json_file(archive, filename)
                    if not isinstance(value, list):
                        errors.append({"code": "automation_shape", "file": filename, "message": "Automation file must contain an array"})
                    else:
                        counts[filename] = len(value)
                        expected = descriptors.get(filename, {}).get("records")
                        if expected is not None and int(expected) != len(value):
                            errors.append({"code": "record_count", "file": filename, "message": f"manifest={expected}, actual={len(value)}"})
                        for index, item in enumerate(value):
                            if not isinstance(item, dict):
                                errors.append({"code": "automation_record", "file": filename, "line": index + 1, "message": "Each automation record must be an object"})
                                continue
                            purpose_key = item.get("purpose_key")
                            if purpose_key is not None and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", str(purpose_key)):
                                errors.append({"code": "purpose_key", "file": filename, "line": index + 1, "message": "purpose_key is invalid"})
                            identity = item.get("external_id") or item.get("event_name") or item.get("slug") or item.get("external_key") or item.get("name")
                            if not identity:
                                errors.append({"code": "automation_identity", "file": filename, "line": index + 1, "message": "external_id or a stable natural key is required"})

                lifecycle = manifest.get("lifecycle_model")
                if lifecycle is not None:
                    if not isinstance(lifecycle, dict) or not isinstance(lifecycle.get("definition"), dict):
                        errors.append({"code": "lifecycle_model", "file": "manifest.json", "message": "lifecycle_model.definition must be an object"})
                    else:
                        lifecycle_report = validate_lifecycle_definition(lifecycle["definition"])
                        errors.extend({**item, "file": "manifest.json"} for item in lifecycle_report["errors"])
                        warnings.extend({**item, "file": "manifest.json"} for item in lifecycle_report["warnings"])
                else:
                    warnings.append({"code": "no_lifecycle_model", "file": "manifest.json", "message": "Import will use the current active model"})

            report = {
                "valid": not errors,
                "blocking": bool(errors),
                "errors": errors,
                "warnings": warnings,
                "counts": counts,
                "planned_actions": {
                    "contacts": "upsert by external_id",
                    "events": "historical, processed, no live side effects",
                    "messages": "terminal history only",
                    "automations": "draft/disabled",
                    "lifecycle_model": "validated; explicit activation required",
                },
                "safety": {
                    "external_sends": 0,
                    "live_event_dispatch": 0,
                    "automations_enabled": 0,
                    "auto_merge": 0,
                },
            }
            row.manifest = manifest
            row.record_counts = counts
            row.validation_report = report
            row.validated_at = datetime.utcnow()
            row.status = "ready" if report["valid"] else "blocked"
            row.error_message = None
            self.db.commit()
            return report
        except Exception as exc:
            self.db.rollback()
            failed = self.get(row.project_id, row.id)
            failed.status = "failed"
            failed.error_message = str(exc)
            failed.validation_report = {
                "valid": False,
                "blocking": True,
                "errors": [{"code": "bundle_error", "message": str(exc)}],
                "warnings": [],
            }
            self.db.commit()
            if isinstance(exc, ProjectImportError):
                raise
            raise ProjectImportError(str(exc)) from exc

    @staticmethod
    def _finding_counts(items: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            code = str(item.get("code") or "unknown")
            counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def next_actions(row: ProjectImport) -> list[dict[str, str]]:
        actions = {
            "created": [{"action": "upload", "instruction": "PUT the ZIP bytes to the raw_url returned by create_project_import."}],
            "uploading": [{"action": "poll", "instruction": "Call get_project_import until upload is terminal, then validate."}],
            "uploaded": [{"action": "validate", "instruction": "Call validate_project_import."}],
            "validating": [{"action": "poll", "instruction": "Call get_project_import until validation is ready or blocked."}],
            "ready": [{"action": "preflight", "instruction": "Call apply_project_import with dry_run=true."}],
            "blocked": [{"action": "reconcile", "instruction": "Fix every blocking finding, upload the corrected ZIP, and validate again; never edit Versya through SQL."}],
            "queued": [{"action": "poll", "instruction": "Application is durable and queued. Poll get_project_import."}],
            "applying": [{"action": "poll", "instruction": "Application is running. Poll get_project_import; do not submit another import."}],
            "reconciling": [{"action": "poll", "instruction": "Final ledger reconciliation is running. Poll get_project_import."}],
            "completed": [
                {"action": "audit", "instruction": "Call audit_project_import and require every acceptance gate to pass."},
                {"action": "shadow", "instruction": "If a lifecycle model was imported, review it in shadow before any activation."},
            ],
            "failed": [{"action": "inspect", "instruction": "Inspect error_message and audit; correct or safely retry the same idempotent import."}],
            "canceled": [{"action": "new_import", "instruction": "Create a new import with a new client_import_id if migration should resume."}],
        }
        return actions.get(row.status, [{"action": "inspect", "instruction": "Call get_project_import again."}])

    def preflight(self, row: ProjectImport) -> dict[str, Any]:
        """Revalidate against current project state and return approval-bound gates."""
        if row.status == "completed":
            return self.audit(row)
        if row.status in {"queued", "applying", "reconciling"}:
            return {
                "import_id": row.id,
                "status": row.status,
                "can_apply": False,
                "next_actions": self.next_actions(row),
            }
        try:
            report = self.validate(row)
        except ProjectImportError:
            # Validation failures are data for an agent, not an invitation to
            # inspect logs. ``validate`` has already persisted a safe report.
            self.db.refresh(row)
            report = row.validation_report or {
                "valid": False,
                "blocking": True,
                "errors": [{"code": "bundle_error", "message": row.error_message or "Validation failed"}],
                "warnings": [],
                "counts": {},
                "safety": {},
            }
        safety = report.get("safety") or {}
        gates = {
            "validation_valid": bool(report.get("valid")),
            "blocking_findings": len(report.get("errors") or []),
            "external_sends": int(safety.get("external_sends") or 0),
            "live_event_dispatch": int(safety.get("live_event_dispatch") or 0),
            "automations_enabled": int(safety.get("automations_enabled") or 0),
        }
        can_apply = (
            gates["validation_valid"]
            and gates["blocking_findings"] == 0
            and gates["external_sends"] == 0
            and gates["live_event_dispatch"] == 0
            and gates["automations_enabled"] == 0
        )
        return {
            "import_id": row.id,
            "status": row.status,
            "bundle_checksum": row.bundle_checksum,
            "bundle_size_bytes": row.bundle_size_bytes,
            "validation_fingerprint": validation_fingerprint(report),
            "can_apply": can_apply,
            "gates": gates,
            "counts": report.get("counts") or {},
            "blocking_by_code": self._finding_counts(report.get("errors") or []),
            "warnings_by_code": self._finding_counts(report.get("warnings") or []),
            "report": report,
            "next_actions": self.next_actions(row),
        }

    def enqueue(self, row: ProjectImport, actor_user_id: int | None) -> ProjectImport:
        """Durably request application; a background worker claims it."""
        if row.status == "completed":
            return row
        if row.status in {"queued", "applying", "reconciling"}:
            return row
        if row.status != "ready" or not (row.validation_report or {}).get("valid"):
            raise ProjectImportError("Import must pass validation before it can be queued")
        row.status = "queued"
        row.applied_by_user_id = actor_user_id
        row.apply_requested_at = datetime.utcnow()
        row.error_message = None
        self.db.commit()
        self.db.refresh(row)
        return row

    def audit(self, row: ProjectImport, issue_limit: int = 100) -> dict[str, Any]:
        """Return the source-to-target ledger and machine-checkable acceptance gates."""
        grouped_rows = (
            self.db.query(
                ProjectImportRecord.record_type,
                ProjectImportRecord.status,
                func.count(ProjectImportRecord.id),
            )
            .filter(ProjectImportRecord.import_id == row.id)
            .group_by(ProjectImportRecord.record_type, ProjectImportRecord.status)
            .all()
        )
        ledger: dict[str, dict[str, int]] = {}
        for record_type, status, count in grouped_rows:
            ledger.setdefault(record_type, {})[status] = int(count)
        failed_or_blocked = sum(
            statuses.get("failed", 0) + statuses.get("blocked", 0)
            for statuses in ledger.values()
        )
        pending = sum(statuses.get("pending", 0) for statuses in ledger.values())
        issue_rows = (
            self.db.query(ProjectImportRecord)
            .filter(
                ProjectImportRecord.import_id == row.id,
                ProjectImportRecord.status.in_(("failed", "blocked", "pending")),
            )
            .order_by(ProjectImportRecord.id)
            .limit(max(1, min(issue_limit, 500)))
            .all()
        )
        result = row.reconciliation_report or {}
        safety = {
            "external_sends": int(result.get("external_sends") or 0),
            "live_event_dispatch": int(result.get("live_event_dispatch") or 0),
            "automations_enabled": int(result.get("automations_enabled") or 0),
        }
        gates = {
            "completed": row.status == "completed",
            "validation_valid": bool((row.validation_report or {}).get("valid")),
            "failed_or_blocked_ledger_records": failed_or_blocked,
            "pending_ledger_records_after_completion": pending if row.status == "completed" else None,
            **safety,
        }
        accepted = (
            gates["completed"]
            and gates["validation_valid"]
            and failed_or_blocked == 0
            and pending == 0
            and all(value == 0 for value in safety.values())
        )
        return {
            "import_id": row.id,
            "project_id": row.project_id,
            "status": row.status,
            "accepted": accepted,
            "bundle_checksum": row.bundle_checksum,
            "validation_fingerprint": validation_fingerprint(row.validation_report),
            "source_counts": row.record_counts or {},
            "ledger": ledger,
            "gates": gates,
            "issues": [
                {
                    "record_type": item.record_type,
                    "external_id": item.external_id,
                    "status": item.status,
                    "action": item.action,
                    "error_code": item.error_code,
                    "error_message": item.error_message,
                }
                for item in issue_rows
            ],
            "issues_truncated": failed_or_blocked + pending > len(issue_rows),
            "reconciliation": result,
            "lifecycle_model_id": row.lifecycle_model_id,
            "next_actions": self.next_actions(row),
        }

    def list_records(
        self,
        row: ProjectImport,
        *,
        record_type: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        query = self.db.query(ProjectImportRecord).filter(ProjectImportRecord.import_id == row.id)
        if record_type:
            query = query.filter(ProjectImportRecord.record_type == record_type)
        if status:
            query = query.filter(ProjectImportRecord.status == status)
        total = query.count()
        items = query.order_by(ProjectImportRecord.id).offset(max(0, offset)).limit(max(1, min(limit, 500))).all()
        return {
            "import_id": row.id,
            "items": [
                {
                    "id": item.id,
                    "record_type": item.record_type,
                    "external_id": item.external_id,
                    "status": item.status,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "action": item.action,
                    "error_code": item.error_code,
                    "error_message": item.error_message,
                    "metadata": item.record_metadata,
                    "applied_at": item.applied_at.isoformat() if item.applied_at else None,
                }
                for item in items
            ],
            "page": {"offset": max(0, offset), "limit": max(1, min(limit, 500)), "total": total},
        }

    def _ledger(
        self,
        row: ProjectImport,
        record_type: str,
        external_id: str,
        payload: dict[str, Any],
    ) -> tuple[ProjectImportRecord, bool]:
        checksum = _canonical_checksum(payload)
        existing = self.db.query(ProjectImportRecord).filter(
            ProjectImportRecord.import_id == row.id,
            ProjectImportRecord.record_type == record_type,
            ProjectImportRecord.external_id == external_id,
        ).first()
        if existing:
            if existing.checksum != checksum:
                raise ProjectImportError(f"Source record changed within import: {record_type}/{external_id}")
            return existing, existing.status in {"created", "updated", "unchanged", "skipped"}
        ledger = ProjectImportRecord(
            import_id=row.id,
            project_id=row.project_id,
            record_type=record_type,
            external_id=external_id,
            checksum=checksum,
        )
        self.db.add(ledger)
        self.db.flush()
        return ledger, False

    @staticmethod
    def _mark(ledger: ProjectImportRecord, status: str, target_type: str, target_id: Any, action: str) -> None:
        ledger.status = status
        ledger.target_type = target_type
        ledger.target_id = str(target_id) if target_id is not None else None
        ledger.action = action
        ledger.applied_at = datetime.utcnow()

    @staticmethod
    def _merge_contact(user: MessagingUser, payload: dict[str, Any], mode: str, authoritative: set[str]) -> bool:
        changed = False
        for field in CONTACT_FIELDS - {"properties", "opted_out_channels", "global_opt_out", "consent_marketing", "consent_analytics"}:
            if field not in payload:
                continue
            value = payload[field]
            if field.endswith("_at"):
                value = _parse_datetime(value, field)
            current = getattr(user, field)
            should_write = (mode == "authoritative_fields" and field in authoritative) or current in (None, "", [])
            if should_write and current != value:
                setattr(user, field, value)
                changed = True

        incoming_properties = payload.get("properties")
        if isinstance(incoming_properties, dict):
            current = dict(user.properties or {})
            if mode == "authoritative_fields" and "properties" in authoritative:
                merged = dict(incoming_properties)
            else:
                merged = {**incoming_properties, **current}
                if mode == "authoritative_fields":
                    for field in authoritative:
                        match = AUTHORITATIVE_PROPERTY_NAMESPACE_RE.fullmatch(str(field))
                        if not match:
                            continue
                        namespace = match.group(1)
                        if namespace in incoming_properties:
                            merged[namespace] = incoming_properties[namespace]
            if merged != current:
                user.properties = merged
                changed = True

        incoming_channels = set(payload.get("opted_out_channels") or [])
        restrictive_channels = sorted(set(user.opted_out_channels or []) | incoming_channels)
        if restrictive_channels != (user.opted_out_channels or []):
            user.opted_out_channels = restrictive_channels
            changed = True
        if payload.get("global_opt_out") is True and not user.global_opt_out:
            user.global_opt_out = True
            changed = True
        for field in ("consent_marketing", "consent_analytics"):
            if payload.get(field) is False and getattr(user, field) is not False:
                setattr(user, field, False)
                changed = True
            elif getattr(user, field) is None and field in payload:
                setattr(user, field, bool(payload[field]))
                changed = True
        return changed

    def _apply_contacts(self, archive: zipfile.ZipFile, row: ProjectImport, authoritative: set[str], result: dict[str, int]) -> dict[str, MessagingUser]:
        all_users = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == row.project_id
        ).all()
        users, _, unresolved = self._canonical_contact_maps(all_users)
        for _, payload in BundleReader.ndjson(archive, "contacts.ndjson"):
            external_id = str(payload["external_id"])
            ledger, done = self._ledger(row, "contact", external_id, payload)
            if done:
                continue
            user = users.get(external_id)
            if user is None:
                if external_id in unresolved:
                    raise ProjectImportError(
                        f"Contact {external_id!r} exists but has no active non-sandbox canonical target"
                    )
                values = {key: value for key, value in payload.items() if key in CONTACT_FIELDS}
                for field in ("first_seen_at", "last_seen_at", "consent_given_at"):
                    if field in values:
                        values[field] = _parse_datetime(values[field], field)
                user = MessagingUser(project_id=row.project_id, external_id=external_id, created_via="import", **values)
                self.db.add(user)
                self.db.flush()
                users[external_id] = user
                status = action = "created"
            else:
                changed = self._merge_contact(user, payload, row.import_mode, authoritative)
                status = "updated" if changed else "unchanged"
                action = status
            self._mark(ledger, status, "messaging_user", user.id, action)
            result[status] = result.get(status, 0) + 1
        return users

    def _apply_permissions(self, archive: zipfile.ZipFile, row: ProjectImport, users: dict[str, MessagingUser], result: dict[str, int]) -> None:
        if "permissions.ndjson" not in archive.namelist():
            return
        for _, payload in BundleReader.ndjson(archive, "permissions.ndjson"):
            ledger, done = self._ledger(row, "permission", str(payload["external_id"]), payload)
            if done:
                continue
            user = users[str(payload["contact_external_id"])]
            changed = self._merge_contact(user, payload, "authoritative_fields", {
                "consent_marketing", "consent_analytics", "consent_given_at", "consent_version",
                "global_opt_out", "opted_out_channels", "is_subscribed",
            })
            status = "updated" if changed else "unchanged"
            self._mark(ledger, status, "messaging_user", user.id, "permission_merge")
            result[status] = result.get(status, 0) + 1

    def _apply_events(self, archive: zipfile.ZipFile, row: ProjectImport, users: dict[str, MessagingUser], result: dict[str, int]) -> None:
        if "events.ndjson" not in archive.namelist():
            return
        for _, payload in BundleReader.ndjson(archive, "events.ndjson"):
            external_id = str(payload["external_id"])
            ledger, done = self._ledger(row, "event", external_id, payload)
            if done:
                continue
            existing = self.db.query(MessagingEvent).filter(
                MessagingEvent.project_id == row.project_id,
                MessagingEvent.external_event_id == external_id,
                MessagingEvent.import_id.isnot(None),
                MessagingEvent.processing_mode.in_(("derive_only", "audit_only")),
            ).order_by(MessagingEvent.id).first()
            if existing is not None:
                self._mark(
                    ledger,
                    "unchanged",
                    "messaging_event",
                    existing.id,
                    "existing_imported_history",
                )
                result["unchanged"] = result.get("unchanged", 0) + 1
                continue
            user = users[str(payload["contact_external_id"])]
            semantic_kind = str(payload.get("semantic_kind") or "fact")
            occurred_at = _parse_datetime(payload.get("occurred_at"), "occurred_at", required=True)
            event = MessagingEvent(
                project_id=row.project_id,
                user_id=user.id,
                event_name=str(payload["event_name"]),
                properties=payload.get("properties") or {},
                source="import",
                processed=True,
                processing_notes={"project_import_id": row.id, "historical": True, "side_effects": False},
                created_at=occurred_at,
                client_ts=occurred_at.replace(tzinfo=timezone.utc) if occurred_at else None,
                occurred_at=occurred_at,
                external_event_id=external_id,
                import_id=row.id,
                processing_mode="derive_only" if semantic_kind == "fact" else "audit_only",
                campaign_origin=payload.get("campaign_origin"),
                attribution=payload.get("attribution"),
            )
            self.db.add(event)
            self.db.flush()
            self._mark(ledger, "created", "messaging_event", event.id, event.processing_mode)
            result["created"] = result.get("created", 0) + 1

    def _apply_messages(self, archive: zipfile.ZipFile, row: ProjectImport, users: dict[str, MessagingUser], result: dict[str, int]) -> None:
        if "messages.ndjson" not in archive.namelist():
            return
        for _, payload in BundleReader.ndjson(archive, "messages.ndjson"):
            external_id = str(payload["external_id"])
            ledger, done = self._ledger(row, "message", external_id, payload)
            if done:
                continue
            existing = self.db.query(SendLog).filter(
                SendLog.project_id == row.project_id,
                SendLog.external_message_id == external_id,
                SendLog.import_id.isnot(None),
                SendLog.is_historical.is_(True),
                SendLog.source_type == "project_import",
            ).order_by(SendLog.id).first()
            if existing is not None:
                self._mark(
                    ledger,
                    "unchanged",
                    "send_log",
                    existing.id,
                    "existing_imported_history",
                )
                result["unchanged"] = result.get("unchanged", 0) + 1
                continue
            user = users[str(payload["contact_external_id"])]
            occurred_at = _parse_datetime(payload.get("occurred_at"), "occurred_at", required=True)
            status = str(payload["status"]).lower().replace("cancelled", "canceled")
            recipient = str(payload.get("recipient") or user.email or user.phone_e164 or user.phone or "historical:unknown")
            send = SendLog(
                project_id=row.project_id,
                user_id=user.id,
                channel=str(payload.get("channel") or "unknown"),
                recipient=recipient,
                content_type=str(payload.get("content_type") or "historical"),
                content_summary=payload.get("content_summary"),
                content_payload=payload.get("content_payload"),
                source_type="project_import",
                decision_trace={"project_import_id": row.id, "historical": True, "side_effects": False},
                status=status,
                provider_message_id=payload.get("provider_message_id"),
                queued_at=occurred_at,
                sent_at=occurred_at if status in {"sent", "delivered", "read"} else None,
                delivered_at=occurred_at if status in {"delivered", "read"} else None,
                read_at=occurred_at if status == "read" else None,
                failed_at=occurred_at if status in {"failed", "bounced"} else None,
                import_id=row.id,
                external_message_id=external_id,
                is_historical=True,
            )
            self.db.add(send)
            self.db.flush()
            self._mark(ledger, "created", "send_log", send.id, "terminal_history")
            result["created"] = result.get("created", 0) + 1

    def _apply_event_schemas(self, archive: zipfile.ZipFile, row: ProjectImport, result: dict[str, int]) -> None:
        name = "automations/event_schemas.json"
        if name not in archive.namelist():
            return
        for payload in BundleReader.json_file(archive, name):
            event_name = str(payload.get("event_name") or "")
            external_id = str(payload.get("external_id") or event_name)
            ledger, done = self._ledger(row, "event_schema", external_id, payload)
            if done:
                continue
            schema = self.db.query(MessagingEventSchema).filter(
                MessagingEventSchema.project_id == row.project_id,
                MessagingEventSchema.event_name == event_name,
            ).first()
            if schema is None:
                schema = MessagingEventSchema(project_id=row.project_id, event_name=event_name)
                self.db.add(schema)
                action = status = "created"
            else:
                action = status = "updated"
            for field in ("display_name", "description", "category", "properties_schema", "required_properties", "semantic_kind", "contract_status", "replacement_event_name"):
                if field in payload:
                    setattr(schema, field, payload[field])
            schema.is_active = False
            self.db.flush()
            self._mark(ledger, status, "messaging_event_schema", schema.id, action)
            result[status] = result.get(status, 0) + 1

    def _apply_templates(self, archive: zipfile.ZipFile, row: ProjectImport, result: dict[str, int]) -> dict[str, MessagingTemplate]:
        name = "automations/templates.json"
        templates: dict[str, MessagingTemplate] = {}
        if name not in archive.namelist():
            return templates
        for payload in BundleReader.json_file(archive, name):
            external_id = str(payload.get("external_id") or payload.get("slug") or "")
            ledger, done = self._ledger(row, "template", external_id, payload)
            slug = str(payload.get("slug") or external_id)
            locale = payload.get("locale")
            template = self.db.query(MessagingTemplate).filter(
                MessagingTemplate.project_id == row.project_id,
                MessagingTemplate.slug == slug,
                MessagingTemplate.locale == locale,
            ).first()
            if done and template:
                templates[external_id] = template
                continue
            if template is None:
                template = MessagingTemplate(
                    project_id=row.project_id,
                    slug=slug,
                    name=str(payload.get("name") or slug),
                    body=str(payload.get("body") or ""),
                )
                self.db.add(template)
                action = status = "created"
            else:
                action = status = "updated"
            for field in (
                "name", "subject", "channel_type", "body", "body_format",
                "locale", "source_locale", "template_metadata",
                "meta_template_name", "meta_language", "meta_components",
                "trigger_events", "purpose_key", "attention_policy",
            ):
                if field in payload:
                    setattr(template, field, payload[field])
            template.is_active = False
            template.automation_enabled = False
            template.external_source = str(payload.get("external_source") or row.source_system)[:20]
            self.db.flush()
            templates[external_id] = template
            self._mark(ledger, status, "messaging_template", template.id, action)
            result[status] = result.get(status, 0) + 1
        return templates

    def _apply_event_actions(self, archive: zipfile.ZipFile, row: ProjectImport, result: dict[str, int]) -> None:
        name = "automations/event_actions.json"
        if name not in archive.namelist():
            return
        for payload in BundleReader.json_file(archive, name):
            external_id = str(payload.get("external_id") or payload.get("name") or "")
            ledger, done = self._ledger(row, "event_action", external_id, payload)
            if done:
                continue
            action = EventAction(
                project_id=row.project_id,
                name=str(payload.get("name") or external_id),
                description=payload.get("description"),
                trigger_event=str(payload.get("trigger_event") or ""),
                purpose_key=payload.get("purpose_key"),
                conditions=payload.get("conditions") or [],
                actions=payload.get("actions") or [],
                stop_conditions=payload.get("stop_conditions") or [],
                is_active=False,
                priority=int(payload.get("priority") or 0),
                cooldown_seconds=int(payload.get("cooldown_seconds") or 86400),
                react_to_delivery=bool(payload.get("react_to_delivery", False)),
                lane=str(payload.get("lane") or "promotional"),
            )
            self.db.add(action)
            self.db.flush()
            self._mark(ledger, "created", "event_action", action.id, "created_disabled")
            result["created"] = result.get("created", 0) + 1

    def _apply_funnels(self, archive: zipfile.ZipFile, row: ProjectImport, actor_user_id: int | None, result: dict[str, int]) -> None:
        name = "automations/funnels.json"
        if name not in archive.namelist():
            return
        for payload in BundleReader.json_file(archive, name):
            if payload.get("is_system") is True:
                continue
            external_id = str(payload.get("external_id") or payload.get("name") or "")
            ledger, done = self._ledger(row, "funnel", external_id, payload)
            if done:
                continue
            funnel = Funnel(
                project_id=row.project_id,
                name=str(payload.get("name") or external_id),
                description=payload.get("description"),
                status="draft",
                trigger_type=str(payload.get("trigger_type") or "event"),
                trigger_config=payload.get("trigger_config") or {},
                global_exit_config=payload.get("global_exit_config") or {},
                purpose_key=payload.get("purpose_key"),
                debug_mode=True,
                source="user",
                is_system=False,
                cooldown_seconds=payload.get("cooldown_seconds"),
                react_to_delivery=bool(payload.get("react_to_delivery", False)),
                created_by=actor_user_id,
            )
            self.db.add(funnel)
            self.db.flush()
            steps = payload.get("steps") or []
            created_steps: dict[str, FunnelStep] = {}
            for index, step_payload in enumerate(steps):
                from app.services.funnel_service import FunnelService
                FunnelService.validate_step_config(
                    str(step_payload.get("step_type") or "wait"),
                    step_payload.get("step_config") or {},
                )
                step_external = str(step_payload.get("external_id") or step_payload.get("slot_id") or index)
                step = FunnelStep(
                    funnel_id=funnel.id,
                    step_type=str(step_payload.get("step_type") or "wait"),
                    step_config=step_payload.get("step_config") or {},
                    position=int(step_payload.get("position", index)),
                    branch=str(step_payload.get("branch") or "main"),
                    slot_id=step_payload.get("slot_id") or str(uuid.uuid4()),
                )
                self.db.add(step)
                self.db.flush()
                created_steps[step_external] = step
            for index, step_payload in enumerate(steps):
                parent = step_payload.get("parent_external_id")
                if parent is not None:
                    step_external = str(step_payload.get("external_id") or step_payload.get("slot_id") or index)
                    created_steps[step_external].parent_step_id = created_steps[str(parent)].id
            self._mark(ledger, "created", "funnel", funnel.id, "created_draft_debug")
            result["created"] = result.get("created", 0) + 1

    def _apply_campaigns(self, archive: zipfile.ZipFile, row: ProjectImport, actor_user_id: int | None, result: dict[str, int]) -> None:
        name = "automations/campaigns.json"
        if name not in archive.namelist():
            return
        for payload in BundleReader.json_file(archive, name):
            external_id = str(payload.get("external_id") or payload.get("external_key") or payload.get("name") or "")
            ledger, done = self._ledger(row, "campaign", external_id, payload)
            if done:
                continue
            campaign = Campaign(
                project_id=row.project_id,
                name=str(payload.get("name") or external_id),
                description=payload.get("description"),
                campaign_type=str(payload.get("campaign_type") or "one_off"),
                status="draft",
                default_channel=str(payload.get("default_channel") or "email"),
                selection_config=payload.get("selection_config") or {},
                policy_config=payload.get("policy_config") or {},
                recurrence_config=payload.get("recurrence_config"),
                timezone=payload.get("timezone"),
                external_key=f"import:{row.source_system}:{external_id}"[:255],
                purpose_key=payload.get("purpose_key"),
                created_by_user_id=actor_user_id,
            )
            self.db.add(campaign)
            self.db.flush()
            for index, action_payload in enumerate(payload.get("actions") or []):
                action = CampaignAction(
                    project_id=row.project_id,
                    campaign_id=campaign.id,
                    position=int(action_payload.get("position", index)),
                    action_type=str(action_payload.get("action_type") or "send"),
                    channel=action_payload.get("channel"),
                    config=action_payload.get("config") or {},
                    status="inactive",
                )
                self.db.add(action)
                self.db.flush()
                for variant_payload in action_payload.get("variants") or []:
                    self.db.add(CampaignVariant(
                        project_id=row.project_id,
                        campaign_id=campaign.id,
                        action_id=action.id,
                        variant_key=str(variant_payload.get("variant_key") or "control"),
                        locale=variant_payload.get("locale"),
                        subject=variant_payload.get("subject"),
                        body=variant_payload.get("body"),
                        variant_config=variant_payload.get("variant_config") or {},
                        weight=int(variant_payload.get("weight") or 100),
                        status="inactive",
                    ))
            self._mark(ledger, "created", "campaign", campaign.id, "created_draft")
            result["created"] = result.get("created", 0) + 1

    def _apply_lifecycle(self, row: ProjectImport, actor_user_id: int | None, result: dict[str, int]) -> LifecycleModel | None:
        lifecycle = (row.manifest or {}).get("lifecycle_model")
        if not lifecycle:
            return self.db.query(LifecycleModel).filter(
                LifecycleModel.project_id == row.project_id,
                LifecycleModel.status == "active",
            ).first()
        payload = lifecycle["definition"]
        external_id = str(lifecycle.get("external_id") or lifecycle.get("name") or "lifecycle-model")
        ledger, done = self._ledger(row, "lifecycle_model", external_id, lifecycle)
        if done and row.lifecycle_model_id:
            return self.db.query(LifecycleModel).filter(LifecycleModel.id == row.lifecycle_model_id).first()
        existing = self.db.query(LifecycleModel).filter(
            LifecycleModel.project_id == row.project_id,
            LifecycleModel.checksum == canonical_checksum(payload),
        ).order_by(LifecycleModel.version.desc()).first()
        if existing is not None:
            row.lifecycle_model_id = existing.id
            self._mark(
                ledger,
                "unchanged",
                "lifecycle_model",
                existing.id,
                "existing_lifecycle_checksum",
            )
            result["unchanged"] = result.get("unchanged", 0) + 1
            return existing
        model = LifecycleModelService(self.db).create(
            row.project_id,
            name=str(lifecycle.get("name") or "Imported lifecycle model"),
            definition=payload,
            actor_user_id=actor_user_id,
            source_import_id=row.id,
            requested_status="validated",
        )
        row.lifecycle_model_id = model.id
        self._mark(ledger, "created", "lifecycle_model", model.id, "created_validated")
        result["created"] = result.get("created", 0) + 1
        return model

    def _apply_positions(self, archive: zipfile.ZipFile, row: ProjectImport, users: dict[str, MessagingUser], model: LifecycleModel | None, result: dict[str, int]) -> None:
        if not model:
            return
        from app.models import ContactPosition, ContactPositionTransition

        if "position_snapshots.ndjson" in archive.namelist():
            grouped: dict[int, tuple[MessagingUser, list[dict[str, Any]]]] = {}
            for _, payload in BundleReader.ndjson(archive, "position_snapshots.ndjson"):
                user = users[str(payload["contact_external_id"])]
                grouped.setdefault(user.id, (user, []))[1].append(payload)

            for user, payloads in grouped.values():
                selected = next(
                    (
                        payload for payload in payloads
                        if str(payload["contact_external_id"]) == user.external_id
                    ),
                    min(payloads, key=lambda payload: str(payload["contact_external_id"])),
                )
                pending: list[tuple[dict[str, Any], ProjectImportRecord]] = []
                for payload in payloads:
                    external_id = str(payload["external_id"])
                    ledger, done = self._ledger(row, "position_snapshot", external_id, payload)
                    if not done:
                        pending.append((payload, ledger))
                if not pending:
                    continue
                position = self.db.query(ContactPosition).filter(
                    ContactPosition.project_id == row.project_id,
                    ContactPosition.user_id == user.id,
                    ContactPosition.lifecycle_model_id == model.id,
                ).first()
                if position is None:
                    position = ContactPosition(project_id=row.project_id, user_id=user.id, lifecycle_model_id=model.id)
                    self.db.add(position)
                    status = "created"
                else:
                    status = "updated"
                position.type = str(selected.get("type") or "default")
                position.stage = selected.get("stage")
                position.age_bucket = selected.get("age_bucket")
                position.position_entered_at = _parse_datetime(selected.get("position_entered_at"), "position_entered_at")
                position.type_entered_at = _parse_datetime(selected.get("type_entered_at"), "type_entered_at") or position.position_entered_at
                position.stage_entered_at = _parse_datetime(selected.get("stage_entered_at"), "stage_entered_at")
                position.computed_at = _parse_datetime(selected.get("as_of"), "as_of") or datetime.utcnow()
                position.provenance = {
                    "project_import_id": row.id,
                    "source_external_id": str(selected["external_id"]),
                    "historical_snapshot": True,
                    "coalesced_source_snapshots": len(payloads),
                }
                position.explanation = selected.get("explanation")
                self.db.flush()
                selected_external_id = str(selected["external_id"])
                for payload, ledger in pending:
                    if str(payload["external_id"]) == selected_external_id:
                        ledger_status = status
                        action = "historical_snapshot"
                    else:
                        ledger_status = "unchanged"
                        action = "coalesced_alias_snapshot"
                    self._mark(ledger, ledger_status, "contact_position", position.id, action)
                    result[ledger_status] = result.get(ledger_status, 0) + 1

        if "position_transitions.ndjson" in archive.namelist():
            for _, payload in BundleReader.ndjson(archive, "position_transitions.ndjson"):
                external_id = str(payload["external_id"])
                ledger, done = self._ledger(row, "position_transition", external_id, payload)
                if done:
                    continue
                user = users[str(payload["contact_external_id"])]
                transition = ContactPositionTransition(
                    project_id=row.project_id,
                    user_id=user.id,
                    lifecycle_model_id=model.id,
                    from_type=payload.get("from_type"),
                    to_type=payload.get("to_type"),
                    from_stage=payload.get("from_stage"),
                    to_stage=payload.get("to_stage"),
                    reason=str(payload.get("reason") or "historical_import")[:50],
                    occurred_at=_parse_datetime(payload.get("occurred_at"), "occurred_at", required=True),
                    provenance={"project_import_id": row.id, "source_external_id": external_id, "historical": True},
                )
                self.db.add(transition)
                self.db.flush()
                self._mark(ledger, "created", "contact_position_transition", transition.id, "historical_transition")
                result["created"] = result.get("created", 0) + 1

    def apply(
        self,
        row: ProjectImport,
        actor_user_id: int | None,
        *,
        already_claimed: bool = False,
    ) -> dict[str, Any]:
        if row.status == "completed":
            return row.reconciliation_report or {"status": "completed", "idempotent": True}
        expected_status = "applying" if already_claimed else "ready"
        if row.status != expected_status or not (row.validation_report or {}).get("valid"):
            raise ProjectImportError("Import must pass validation before apply")
        if not row.bundle_storage_key:
            raise ProjectImportError("Bundle is not available")
        row.status = "applying"
        if actor_user_id is not None:
            row.applied_by_user_id = actor_user_id
        row.applied_at = datetime.utcnow()
        self.db.commit()
        result: dict[str, Any] = {
            "contacts": {}, "permissions": {}, "events": {}, "messages": {},
            "event_schemas": {}, "templates": {}, "event_actions": {}, "funnels": {},
            "campaigns": {}, "lifecycle_models": {}, "positions": {},
            "external_sends": 0, "live_event_dispatch": 0,
            "automations_enabled": 0,
        }
        try:
            reader = BundleReader(self.storage.get_local_path(row.bundle_storage_key))
            archive, _ = reader.inspect()
            with archive:
                authoritative = set((row.manifest or {}).get("authoritative_fields") or [])
                users = self._apply_contacts(archive, row, authoritative, result["contacts"])
                self._apply_permissions(archive, row, users, result["permissions"])
                self._apply_events(archive, row, users, result["events"])
                self._apply_messages(archive, row, users, result["messages"])
                self._apply_event_schemas(archive, row, result["event_schemas"])
                self._apply_templates(archive, row, result["templates"])
                self._apply_event_actions(archive, row, result["event_actions"])
                self._apply_funnels(archive, row, actor_user_id, result["funnels"])
                self._apply_campaigns(archive, row, actor_user_id, result["campaigns"])
                model = self._apply_lifecycle(row, actor_user_id, result["lifecycle_models"])
                self._apply_positions(archive, row, users, model, result["positions"])
            row.status = "reconciling"
            self.db.flush()
            ledger_counts: dict[str, dict[str, int]] = {}
            for record in self.db.query(ProjectImportRecord).filter(ProjectImportRecord.import_id == row.id).all():
                bucket = ledger_counts.setdefault(record.record_type, {})
                bucket[record.status] = bucket.get(record.status, 0) + 1
            result["ledger"] = ledger_counts
            result["completed_at"] = datetime.utcnow().isoformat()
            result["idempotent"] = True
            row.reconciliation_report = result
            row.status = "completed"
            row.completed_at = datetime.utcnow()
            self.db.commit()
            return result
        except Exception as exc:
            self.db.rollback()
            failed = self.get(row.project_id, row.id)
            failed.status = "failed"
            failed.error_message = str(exc)
            self.db.commit()
            if isinstance(exc, ProjectImportError):
                raise
            raise ProjectImportError(str(exc)) from exc
