"""Validation, evaluation and activation for versioned lifecycle models."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ContactPosition, ContactPositionTransition
from app.models.messaging import MessagingEvent, MessagingUser
from app.models.project_import import LifecycleModel
from app.services.contact_groups.compiler import (
    GroupFilterError,
    compile_condition,
    compile_group_query,
    normalized_rule,
)


KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
LEGACY_DEFINITION = {
    "contract_version": "1.0",
    "key": "legacy-v1",
    "types": [{"key": "default", "label": "Default", "when": {"filters": []}, "catch_all": True}],
    "stages": {"default": []},
    "age_buckets": [
        {"key": "first_hours", "max_seconds": 86400},
        {"key": "first_days", "max_seconds": 604800},
        {"key": "first_weeks", "max_seconds": 2592000},
        {"key": "settled", "max_seconds": 31536000},
        {"key": "veteran"},
    ],
}


LIFECYCLE_CONTRACT = {
    "contract": "versya.lifecycle-model",
    "contract_version": "1.0",
    "semantics": {
        "lifecycle": (
            "A versioned, project-scoped deterministic classifier that assigns every active "
            "non-sandbox contact one Position: (Type, Stage, Age)."
        ),
        "type": "A durable commercial relationship; changing Type resets the Type-entry clock.",
        "stage": "The one current operational condition inside the selected Type.",
        "age": "A bucket derived from elapsed time since Type entry, not Stage entry or last activity.",
        "position": (
            "The materialized Type, Stage and Age for one contact/model, with entry timestamps, "
            "calculation time, provenance and matched-rule explanation."
        ),
        "distribution": (
            "Counts and coverage by Type, Type/Stage and Type/Stage/Age, including contacts "
            "without a materialized Position."
        ),
        "contact_explanation": (
            "The first matching Type rule and Stage rule, their priorities and conditions, plus "
            "the stored-versus-current result for one contact."
        ),
    },
    "canonical": {
        "platform_axes": ["type", "stage", "age"],
        "model_statuses": ["draft", "validated", "shadow", "active", "archived"],
        "cutover_modes": ["legacy", "shadow", "versya"],
        "rule_semantics": "first_match_wins",
        "entry_clock": "type_entered_at",
    },
    "project_scoped": {
        "type_keys": "Tenant taxonomy; not globally canonical.",
        "stage_keys": "Tenant taxonomy within a Type; not globally canonical.",
        "age_bucket_keys": "Tenant taxonomy over the canonical Type-entry clock.",
        "recommended_baseline_only": {
            "prospect": ["lead_captured", "registered", "trial_new", "trial_activated", "trial_expiring", "trial_exhausted", "trial_expired"],
            "customer": ["active", "renewal_due", "payment_issue"],
            "former_customer": ["canceled", "expired"],
            "unknown": ["unclassified"],
        },
    },
    "authoring_questions": [
        "Which source facts are authoritative, and how are they kept current?",
        "Which commercial relationships are durable enough to be Types?",
        "Which mutually exclusive operational conditions are Stages inside each Type?",
        "When several facts conflict, which rule wins?",
        "What is the catch-all Type and catch-all Stage for each Type?",
        "What moment starts the Type-entry clock and which Age buckets are useful?",
        "Which business purposes will own campaigns or funnels, and what are their goals and stop conditions?",
    ],
    "supported_rule_inputs": [
        "contact fields (for example first_seen_at, last_seen_at, consent_marketing)",
        "property.<namespaced.path>",
        "tag or tags",
        "event / event_performed",
        "event_aggregate / feature",
        "verification.<type>.<attribute>",
        "score.<definition_id>.<score|tier>",
        "thermal_state / journey.thermal_state",
    ],
    "invariants": [
        "Exactly one catch-all Type is last.",
        "Exactly one catch-all Stage is last for every Type.",
        "Rules cannot depend on position.* because that would be recursive.",
        "A model must be evaluated in shadow before it can become active.",
        "Shadow materialization emits no transition events and sends no messages.",
    ],
}


def lifecycle_contract_document() -> dict[str, Any]:
    """Return a self-describing copy safe for MCP and documentation clients."""
    return json.loads(json.dumps(LIFECYCLE_CONTRACT))


class LifecycleModelError(ValueError):
    pass


def canonical_checksum(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rule_config(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("when") or {"filters": []}


def _validate_rule_config(config: dict[str, Any]) -> None:
    _mode, filters = normalized_rule(config)
    for condition in filters:
        field = str(condition.get("field") or "")
        if field.startswith("position."):
            raise GroupFilterError("Lifecycle rules cannot depend on their own materialized position")
        try:
            # Building the expression validates field names, operators and
            # typed values without issuing a database query.
            compile_condition(0, condition, datetime.utcnow())
        except GroupFilterError:
            raise
        except (TypeError, ValueError) as exc:
            raise GroupFilterError(str(exc)) from exc


def validate_lifecycle_definition(definition: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if definition.get("contract_version") != "1.0":
        errors.append({"code": "contract_version", "message": "lifecycle contract_version must be '1.0'"})

    types = definition.get("types")
    if not isinstance(types, list) or not types:
        errors.append({"code": "types_required", "message": "types must be a non-empty array"})
        types = []

    seen: set[str] = set()
    catch_all = []
    for index, item in enumerate(types):
        path = f"types[{index}]"
        if not isinstance(item, dict):
            errors.append({"code": "invalid_type", "message": f"{path} must be an object"})
            continue
        key = str(item.get("key") or "")
        if not KEY_RE.fullmatch(key):
            errors.append({"code": "invalid_key", "message": f"{path}.key is invalid"})
        if key in seen:
            errors.append({"code": "duplicate_type", "message": f"duplicate Type key: {key}"})
        seen.add(key)
        if item.get("catch_all") is True:
            catch_all.append(index)
        try:
            _validate_rule_config(_rule_config(item))
        except GroupFilterError as exc:
            errors.append({"code": "invalid_type_rule", "message": f"{path}: {exc}"})

    if len(catch_all) != 1:
        errors.append({"code": "type_catch_all", "message": "exactly one Type must be catch_all"})
    elif catch_all[0] != len(types) - 1:
        errors.append({"code": "type_precedence", "message": "the catch_all Type must be last"})

    stages = definition.get("stages")
    if not isinstance(stages, dict):
        errors.append({"code": "stages_required", "message": "stages must be an object keyed by Type"})
        stages = {}
    unknown_stage_types = sorted(set(stages) - seen)
    if unknown_stage_types:
        errors.append({"code": "unknown_stage_type", "message": "stages reference unknown Types: " + ", ".join(unknown_stage_types)})

    for type_key in seen:
        rules = stages.get(type_key)
        if not isinstance(rules, list) or not rules:
            errors.append({"code": "stage_rules_required", "message": f"Type {type_key} requires ordered Stage rules"})
            continue
        stage_seen: set[str] = set()
        stage_catch_all: list[int] = []
        for index, item in enumerate(rules):
            path = f"stages.{type_key}[{index}]"
            if not isinstance(item, dict):
                errors.append({"code": "invalid_stage", "message": f"{path} must be an object"})
                continue
            key = str(item.get("key") or "")
            if not KEY_RE.fullmatch(key):
                errors.append({"code": "invalid_key", "message": f"{path}.key is invalid"})
            if key in stage_seen:
                errors.append({"code": "duplicate_stage", "message": f"duplicate Stage key in {type_key}: {key}"})
            stage_seen.add(key)
            if item.get("catch_all") is True:
                stage_catch_all.append(index)
            try:
                _validate_rule_config(_rule_config(item))
            except GroupFilterError as exc:
                errors.append({"code": "invalid_stage_rule", "message": f"{path}: {exc}"})
        if len(stage_catch_all) != 1:
            errors.append({"code": "stage_catch_all", "message": f"Type {type_key} needs exactly one catch_all Stage"})
        elif stage_catch_all[0] != len(rules) - 1:
            errors.append({"code": "stage_precedence", "message": f"catch_all Stage for {type_key} must be last"})

    buckets = definition.get("age_buckets")
    if not isinstance(buckets, list) or not buckets:
        errors.append({"code": "age_buckets_required", "message": "age_buckets must be a non-empty array"})
        buckets = []
    previous = -1
    for index, bucket in enumerate(buckets):
        if not isinstance(bucket, dict) or not KEY_RE.fullmatch(str(bucket.get("key") or "")):
            errors.append({"code": "invalid_age_bucket", "message": f"age_buckets[{index}] is invalid"})
            continue
        maximum = bucket.get("max_seconds")
        if maximum is None:
            if index != len(buckets) - 1:
                errors.append({"code": "age_bucket_order", "message": "only the last Age bucket may omit max_seconds"})
        elif not isinstance(maximum, int) or maximum <= previous:
            errors.append({"code": "age_bucket_order", "message": "Age max_seconds must be strictly increasing positive integers"})
        else:
            previous = maximum

    if definition.get("entry_clock", "type_entered_at") != "type_entered_at":
        warnings.append({"code": "entry_clock_normalized", "message": "Age currently uses type_entered_at"})

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checksum": canonical_checksum(definition),
    }


class LifecycleModelService:
    def __init__(self, db: Session):
        self.db = db

    def list(self, project_id: int) -> list[LifecycleModel]:
        return self.db.query(LifecycleModel).filter(
            LifecycleModel.project_id == project_id,
        ).order_by(LifecycleModel.version.desc()).all()

    def ensure_legacy_model(self, project_id: int, actor_user_id: int | None = None) -> LifecycleModel:
        active = self.db.query(LifecycleModel).filter(
            LifecycleModel.project_id == project_id,
            LifecycleModel.status == "active",
        ).first()
        if active:
            return active
        existing = self.db.query(LifecycleModel).filter(
            LifecycleModel.project_id == project_id,
        ).order_by(LifecycleModel.version).first()
        if existing:
            return existing
        row = LifecycleModel(
            project_id=project_id,
            version=1,
            name="Legacy compatibility model",
            status="active",
            contract_version="1.0",
            definition=LEGACY_DEFINITION,
            checksum=canonical_checksum(LEGACY_DEFINITION),
            validation_report={"valid": True, "bootstrap": True, "errors": [], "warnings": []},
            created_by_user_id=actor_user_id,
            activated_at=datetime.utcnow(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, project_id: int, model_id: int) -> LifecycleModel:
        row = self.db.query(LifecycleModel).filter(
            LifecycleModel.id == model_id,
            LifecycleModel.project_id == project_id,
        ).first()
        if not row:
            raise LifecycleModelError("Lifecycle model not found")
        return row

    def create(
        self,
        project_id: int,
        *,
        name: str,
        definition: dict[str, Any],
        actor_user_id: int | None,
        source_import_id: str | None = None,
        requested_status: str = "draft",
    ) -> LifecycleModel:
        report = validate_lifecycle_definition(definition)
        if requested_status == "validated" and not report["valid"]:
            raise LifecycleModelError("Lifecycle model has blocking validation errors")
        version = (self.db.query(func.max(LifecycleModel.version)).filter(
            LifecycleModel.project_id == project_id,
        ).scalar() or 0) + 1
        row = LifecycleModel(
            project_id=project_id,
            version=version,
            name=name,
            status=requested_status,
            contract_version="1.0",
            definition=definition,
            checksum=report["checksum"],
            validation_report=report,
            source_import_id=source_import_id,
            created_by_user_id=actor_user_id,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def validate(self, model: LifecycleModel) -> dict[str, Any]:
        report = validate_lifecycle_definition(model.definition or {})
        model.validation_report = report
        model.checksum = report["checksum"]
        if report["valid"] and model.status == "draft":
            model.status = "validated"
        self.db.flush()
        return report

    def _ordered_members(
        self,
        project_id: int,
        rules: list[dict[str, Any]],
        universe: set[int],
        as_of: datetime,
    ) -> tuple[dict[int, str], dict[int, dict[str, Any]]]:
        remaining = set(universe)
        assignments: dict[int, str] = {}
        explanations: dict[int, dict[str, Any]] = {}
        for index, rule in enumerate(rules):
            if not remaining:
                break
            if rule.get("catch_all") is True:
                matched = remaining
            else:
                query = compile_group_query(self.db, project_id, _rule_config(rule), now=as_of)
                matched = {int(row[0]) for row in query.filter(MessagingUser.id.in_(remaining)).all()}
            for user_id in matched:
                assignments[user_id] = str(rule["key"])
                explanations[user_id] = {
                    "rule_key": str(rule.get("rule_key") or rule["key"]),
                    "rule_label": str(rule.get("label") or rule["key"]),
                    "priority": index,
                    "catch_all": rule.get("catch_all") is True,
                    "when": _rule_config(rule),
                }
            remaining.difference_update(matched)
        if remaining:
            raise LifecycleModelError(f"Lifecycle rules left {len(remaining)} contacts unclassified")
        return assignments, explanations

    def classify(
        self,
        model: LifecycleModel,
        *,
        as_of: datetime | None = None,
        limit: int | None = None,
        user_ids: set[int] | None = None,
    ) -> dict[int, dict[str, Any]]:
        report = validate_lifecycle_definition(model.definition or {})
        if not report["valid"] and (model.definition or {}).get("key") != "legacy-v1":
            raise LifecycleModelError("Lifecycle model is invalid")
        now = as_of or datetime.utcnow()
        query = self.db.query(MessagingUser.id).filter(
            MessagingUser.project_id == model.project_id,
            MessagingUser.status == "active",
            MessagingUser.is_sandbox == False,  # noqa: E712
        ).order_by(MessagingUser.id)
        if user_ids is not None:
            if not user_ids:
                return {}
            query = query.filter(MessagingUser.id.in_(user_ids))
        if limit:
            query = query.limit(limit)
        universe = {int(row[0]) for row in query.all()}

        definition = model.definition or {}
        if definition.get("key") == "legacy-v1":
            users = self.db.query(MessagingUser).filter(MessagingUser.id.in_(universe)).all() if universe else []
            return {
                user.id: {
                    "type": user.segment_name or "default",
                    "stage": user.lifecycle_stage,
                    "type_explanation": {"rule_key": "legacy.segment_name", "priority": 0},
                    "stage_explanation": {"rule_key": "legacy.lifecycle_stage", "priority": 0},
                }
                for user in users
            }

        type_assignments, type_explanations = self._ordered_members(
            model.project_id, definition["types"], universe, now,
        )
        result: dict[int, dict[str, Any]] = {}
        for type_key in {value for value in type_assignments.values()}:
            members = {uid for uid, value in type_assignments.items() if value == type_key}
            stage_assignments, stage_explanations = self._ordered_members(
                model.project_id, definition["stages"][type_key], members, now,
            )
            for user_id in members:
                result[user_id] = {
                    "type": type_key,
                    "stage": stage_assignments[user_id],
                    "type_explanation": type_explanations[user_id],
                    "stage_explanation": stage_explanations[user_id],
                }
        return result

    @staticmethod
    def _age_bucket(definition: dict[str, Any], entered_at: datetime | None, as_of: datetime) -> str | None:
        if entered_at is None:
            return None
        # Contact timestamps are historically stored as naive UTC. Normalize
        # aware API values before subtraction so comparisons are deterministic.
        if as_of.tzinfo is not None:
            as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
        if entered_at.tzinfo is not None:
            entered_at = entered_at.astimezone(timezone.utc).replace(tzinfo=None)
        seconds = max(0, int((as_of - entered_at).total_seconds()))
        for bucket in definition.get("age_buckets") or []:
            maximum = bucket.get("max_seconds")
            if maximum is None or seconds < int(maximum):
                return str(bucket["key"])
        return None

    def materialize(
        self,
        model: LifecycleModel,
        *,
        as_of: datetime | None = None,
        emit_transitions: bool = False,
        emit_events: bool = False,
        initial_snapshot: bool = False,
    ) -> dict[str, int]:
        now = as_of or datetime.utcnow()
        classified = self.classify(model, as_of=now)
        existing = {
            row.user_id: row for row in self.db.query(ContactPosition).filter(
                ContactPosition.project_id == model.project_id,
                ContactPosition.lifecycle_model_id == model.id,
            ).all()
        }
        inserted = updated = transitions = 0
        for user_id, target in classified.items():
            row = existing.get(user_id)
            if row is None:
                entered_at = now
                user = self.db.query(MessagingUser).filter(MessagingUser.id == user_id).first()
                if user:
                    entered_at = user.first_seen_at or user.created_at or now
                row = ContactPosition(
                    project_id=model.project_id,
                    user_id=user_id,
                    lifecycle_model_id=model.id,
                    type=target["type"],
                    stage=target["stage"],
                    position_entered_at=entered_at,
                    type_entered_at=entered_at,
                    stage_entered_at=entered_at,
                    computed_at=now,
                    provenance={"model_id": model.id, "model_version": model.version, "initial_snapshot": initial_snapshot},
                    explanation=target,
                )
                row.age_bucket = self._age_bucket(model.definition or {}, row.type_entered_at, now)
                self.db.add(row)
                inserted += 1
                continue

            type_changed = row.type != target["type"]
            stage_changed = row.stage != target["stage"]
            if type_changed or stage_changed:
                if emit_transitions and not initial_snapshot:
                    self.db.add(ContactPositionTransition(
                        project_id=model.project_id,
                        user_id=user_id,
                        lifecycle_model_id=model.id,
                        from_type=row.type,
                        to_type=target["type"],
                        from_stage=row.stage,
                        to_stage=target["stage"],
                        reason="model_replay",
                        occurred_at=now,
                        provenance={"model_id": model.id, "model_version": model.version},
                    ))
                    transitions += 1
                if emit_events and not initial_snapshot:
                    self.db.add(MessagingEvent(
                        project_id=model.project_id,
                        user_id=user_id,
                        event_name="position.type_changed" if type_changed else "position.stage_changed",
                        properties={
                            "from_type": row.type,
                            "to_type": target["type"],
                            "from_stage": row.stage,
                            "to_stage": target["stage"],
                            "lifecycle_model_id": model.id,
                            "lifecycle_model_version": model.version,
                        },
                        source="system",
                        processed=False,
                        processing_mode="live",
                        occurred_at=now,
                    ))
                if type_changed:
                    row.type_entered_at = now
                    row.position_entered_at = now
                if stage_changed:
                    row.stage_entered_at = now
                row.type = target["type"]
                row.stage = target["stage"]
                updated += 1
            row.age_bucket = self._age_bucket(model.definition or {}, row.type_entered_at, now)
            row.computed_at = now
            row.provenance = {"model_id": model.id, "model_version": model.version, "initial_snapshot": initial_snapshot}
            row.explanation = target
        self.db.flush()
        return {"classified": len(classified), "inserted": inserted, "updated": updated, "transitions": transitions}

    def compare(
        self,
        left: LifecycleModel,
        right: LifecycleModel,
        *,
        sample_limit: int,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        left_rows = self.classify(left, as_of=as_of, limit=sample_limit)
        right_rows = self.classify(right, as_of=as_of, limit=sample_limit)
        ids = sorted(set(left_rows) | set(right_rows))
        changes = []
        matrix: Counter[str] = Counter()
        for user_id in ids:
            before = left_rows.get(user_id)
            after = right_rows.get(user_id)
            before_key = "missing" if not before else f"{before['type']}/{before['stage']}"
            after_key = "missing" if not after else f"{after['type']}/{after['stage']}"
            matrix[f"{before_key} -> {after_key}"] += 1
            if before_key != after_key:
                changes.append({"user_id": user_id, "from": before, "to": after})
        return {
            "sampled": len(ids),
            "changed": len(changes),
            "unchanged": len(ids) - len(changes),
            "transition_matrix": dict(matrix),
            "examples": changes[:100],
            "as_of": (as_of or datetime.utcnow()).isoformat(),
        }

    @staticmethod
    def _distribution(rows: dict[int, dict[str, Any]]) -> dict[str, dict[str, int]]:
        by_type: Counter[str] = Counter()
        by_type_stage: Counter[str] = Counter()
        by_position: Counter[str] = Counter()
        for value in rows.values():
            type_key = str(value.get("type") or "missing")
            stage_key = str(value.get("stage") or "missing")
            age_key = str(value.get("age_bucket") or "missing")
            by_type[type_key] += 1
            by_type_stage[f"{type_key}/{stage_key}"] += 1
            by_position[f"{type_key}/{stage_key}/{age_key}"] += 1
        return {
            "by_type": dict(sorted(by_type.items())),
            "by_type_stage": dict(sorted(by_type_stage.items())),
            "by_type_stage_age": dict(sorted(by_position.items())),
        }

    def distribution(self, model: LifecycleModel) -> dict[str, Any]:
        eligible_ids = {
            int(row[0]) for row in self.db.query(MessagingUser.id).filter(
                MessagingUser.project_id == model.project_id,
                MessagingUser.status == "active",
                MessagingUser.is_sandbox == False,  # noqa: E712
            ).all()
        }
        positions = self.db.query(ContactPosition).filter(
            ContactPosition.project_id == model.project_id,
            ContactPosition.lifecycle_model_id == model.id,
            ContactPosition.user_id.in_(eligible_ids) if eligible_ids else False,
        ).all()
        rows = {
            int(row.user_id): {
                "type": row.type,
                "stage": row.stage,
                "age_bucket": row.age_bucket,
            }
            for row in positions
        }
        computed_values = [row.computed_at for row in positions if row.computed_at is not None]
        return {
            "model_id": model.id,
            "model_version": model.version,
            "model_status": model.status,
            "eligible_contacts": len(eligible_ids),
            "positioned_contacts": len(rows),
            "missing_positions": len(eligible_ids - set(rows)),
            "coverage_ratio": round(len(rows) / len(eligible_ids), 6) if eligible_ids else 1.0,
            "oldest_computed_at": min(computed_values).isoformat() if computed_values else None,
            "newest_computed_at": max(computed_values).isoformat() if computed_values else None,
            **self._distribution(rows),
        }

    def compare_materialized(
        self,
        model: LifecycleModel,
        *,
        as_of: datetime | None = None,
        example_limit: int = 100,
    ) -> dict[str, Any]:
        now = as_of or datetime.utcnow()
        current = self.classify(model, as_of=now)
        stored_rows = self.db.query(ContactPosition).filter(
            ContactPosition.project_id == model.project_id,
            ContactPosition.lifecycle_model_id == model.id,
        ).all()
        stored_entered_at = {int(row.user_id): row.type_entered_at for row in stored_rows}
        stored = {
            int(row.user_id): {
                "type": row.type,
                "stage": row.stage,
                "age_bucket": row.age_bucket,
                "computed_at": row.computed_at.isoformat() if row.computed_at else None,
                "explanation": row.explanation,
            }
            for row in stored_rows
        }
        initial_entered_at = {
            int(row.id): (row.first_seen_at or row.created_at or now)
            for row in self.db.query(MessagingUser).filter(
                MessagingUser.id.in_(set(current) - set(stored)) if set(current) - set(stored) else False,
            ).all()
        }
        current_with_age: dict[int, dict[str, Any]] = {}
        for user_id, value in current.items():
            if user_id in stored and stored[user_id]["type"] == value["type"]:
                entered_at = stored_entered_at.get(user_id)
            elif user_id in stored:
                entered_at = now
            else:
                entered_at = initial_entered_at.get(user_id, now)
            current_with_age[user_id] = {
                **value,
                "age_bucket": self._age_bucket(model.definition or {}, entered_at, now),
            }
        current = current_with_age
        ids = sorted(set(stored) | set(current))
        matrix: Counter[str] = Counter()
        mismatches: list[dict[str, Any]] = []
        unchanged = 0
        for user_id in ids:
            before = stored.get(user_id)
            after = current.get(user_id)
            before_key = "missing" if before is None else f"{before['type']}/{before['stage']}"
            after_key = "missing" if after is None else f"{after['type']}/{after['stage']}"
            matrix[f"{before_key} -> {after_key}"] += 1
            if before_key == after_key:
                unchanged += 1
            elif len(mismatches) < max(1, min(example_limit, 500)):
                mismatches.append({"user_id": user_id, "stored": before, "current": after})
        return {
            "model_id": model.id,
            "as_of": now.isoformat(),
            "stored_contacts": len(stored),
            "currently_classified_contacts": len(current),
            "compared_contacts": len(ids),
            "unchanged": unchanged,
            "mismatched": len(ids) - unchanged,
            "missing_stored": len(set(current) - set(stored)),
            "missing_current": len(set(stored) - set(current)),
            "transition_matrix": dict(sorted(matrix.items())),
            "stored_distribution": self._distribution(stored),
            "current_distribution": self._distribution(current),
            "examples": mismatches,
        }

    @staticmethod
    def _observed_rule_evidence(user: MessagingUser, explanation: dict[str, Any] | None) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        filters = ((explanation or {}).get("when") or {}).get("filters") or []
        for condition in filters:
            item = {
                "field": condition.get("field"),
                "operator": condition.get("operator", "equals"),
                "expected": condition.get("value"),
                "matched": True,
            }
            field = str(condition.get("field") or "")
            if field.startswith("property."):
                observed: Any = user.properties or {}
                for part in field.split(".")[1:]:
                    observed = observed.get(part) if isinstance(observed, dict) else None
                item["observed"] = observed
            elif field in {"lifecycle_stage", "segment", "segment_name", "created_via", "is_subscribed", "consent_marketing"}:
                item["observed"] = getattr(user, "segment_name" if field == "segment" else field, None)
            evidence.append(item)
        return evidence

    def explain_contact(
        self,
        model: LifecycleModel,
        user: MessagingUser,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        if user.project_id != model.project_id:
            raise LifecycleModelError("Contact does not belong to lifecycle model project")
        now = as_of or datetime.utcnow()
        current = self.classify(model, as_of=now, user_ids={int(user.id)}).get(int(user.id))
        stored = self.db.query(ContactPosition).filter(
            ContactPosition.project_id == model.project_id,
            ContactPosition.lifecycle_model_id == model.id,
            ContactPosition.user_id == user.id,
        ).first()
        stored_value = None if stored is None else {
            "type": stored.type,
            "stage": stored.stage,
            "age_bucket": stored.age_bucket,
            "type_entered_at": stored.type_entered_at.isoformat() if stored.type_entered_at else None,
            "stage_entered_at": stored.stage_entered_at.isoformat() if stored.stage_entered_at else None,
            "computed_at": stored.computed_at.isoformat() if stored.computed_at else None,
            "provenance": stored.provenance,
            "explanation": stored.explanation,
        }
        current_value = None
        if current is not None:
            entered_at = stored.type_entered_at if stored and stored.type == current["type"] else now
            current_value = {
                **current,
                "age_bucket": self._age_bucket(model.definition or {}, entered_at, now),
                "type_evidence": self._observed_rule_evidence(user, current.get("type_explanation")),
                "stage_evidence": self._observed_rule_evidence(user, current.get("stage_explanation")),
            }
        stored_key = None if stored_value is None else (stored_value["type"], stored_value["stage"])
        current_key = None if current_value is None else (current_value["type"], current_value["stage"])
        return {
            "contact": {"id": user.id, "external_id": user.external_id, "status": user.status},
            "model": {"id": model.id, "version": model.version, "status": model.status, "checksum": model.checksum},
            "stored": stored_value,
            "current": current_value,
            "matches_materialized": stored_key == current_key,
            "as_of": now.isoformat(),
        }

    def activate(
        self,
        model: LifecycleModel,
        *,
        target_status: str,
        actor_user_id: int | None,
    ) -> dict[str, Any]:
        report = self.validate(model)
        if not report["valid"]:
            raise LifecycleModelError("Lifecycle model has blocking validation errors")
        if target_status not in {"shadow", "active"}:
            raise LifecycleModelError("target_status must be shadow or active")
        if model.status == "active" and target_status == "shadow":
            raise LifecycleModelError("An active lifecycle model cannot be demoted to shadow")
        if target_status == "active" and model.status not in {"shadow", "active"}:
            raise LifecycleModelError("A lifecycle model must be evaluated in shadow before activation")

        current = self.db.query(LifecycleModel).filter(
            LifecycleModel.project_id == model.project_id,
            LifecycleModel.status == target_status,
            LifecycleModel.id != model.id,
        ).first()
        if current:
            current.status = "validated" if target_status == "shadow" else "archived"
            if target_status == "active":
                current.archived_at = datetime.utcnow()

        materialization = self.materialize(
            model,
            initial_snapshot=not self.db.query(ContactPosition.id).filter(
                ContactPosition.lifecycle_model_id == model.id,
            ).first(),
            emit_transitions=False,
        )
        model.status = target_status
        model.approved_by_user_id = actor_user_id
        model.approved_at = datetime.utcnow()
        if target_status == "active":
            model.activated_at = datetime.utcnow()
        self.db.flush()
        return {"model_id": model.id, "status": model.status, "materialization": materialization}
