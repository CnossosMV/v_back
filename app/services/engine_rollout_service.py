"""Typed per-project resolution for lifecycle-engine rollout switches."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.engine_control import ProjectEngineRollout, ProjectEngineRolloutEvent

MODES = ("inherit", "off", "shadow", "enforce")
MODE_RANK = {mode: index for index, mode in enumerate(MODES)}


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    env: str
    group: str
    kind: str = "mode"
    dependencies: tuple[tuple[str, str], ...] = ()


FEATURES: dict[str, FeatureSpec] = {
    spec.key: spec for spec in (
        FeatureSpec("ledger", "SEND_LEDGER_IN_SEND", "send", "bool"),
        FeatureSpec("consent", "SEND_CONSENT_ENFORCE", "send", "bool"),
        FeatureSpec("retry_policy", "SEND_RETRY_POLICY_ENFORCE", "send", "bool"),
        FeatureSpec("reroute_agent_team_funnel", "SEND_ROUTE_AGENT_TEAM_FUNNEL", "reroute"),
        FeatureSpec("reroute_chatbot_funnel", "SEND_ROUTE_CHATBOT_FUNNEL", "reroute"),
        FeatureSpec("reroute_template", "SEND_ROUTE_TEMPLATE", "reroute"),
        FeatureSpec("guardian", "SEND_GUARDIAN_MODE", "send", dependencies=(("ledger", "enforce"),)),
        FeatureSpec("pace", "SEND_PACE_MODE", "send"),
        FeatureSpec("candidate", "SEND_CANDIDATE_MODE", "selection"),
        FeatureSpec("selection", "SEND_SELECTION_MODE", "selection", dependencies=(
            ("candidate", "enforce"), ("guardian", "enforce"), ("ledger", "enforce"),
            ("consent", "enforce"), ("pace", "enforce"),
        )),
        FeatureSpec("future_plan", "SEND_FUTURE_PLAN_MODE", "selection", dependencies=(
            ("candidate", "enforce"), ("selection", "enforce"),
        )),
        FeatureSpec("supersede", "SEND_SUPERSEDE_ENFORCE", "selection", "bool"),
        FeatureSpec("position", "POSITION_EMIT_TRANSITIONS", "lifecycle", "bool"),
        FeatureSpec("base_compile", "SEND_BASE_COMPILE", "lifecycle", dependencies=(("position", "enforce"), ("candidate", "enforce"))),
        FeatureSpec("bandit_reward", "BANDIT_REWARD_SWEEP", "learning", "bool"),
        FeatureSpec("bandit_act", "SEND_BANDIT_ACT", "learning", dependencies=(("selection", "enforce"), ("bandit_reward", "enforce"))),
        FeatureSpec("locale_resolution", "USE_LOCALE_RESOLUTION", "locale", "bool"),
        FeatureSpec("template_variants", "USE_TEMPLATE_VARIANTS", "locale", "bool"),
        FeatureSpec("locale_channel_routing", "USE_LOCALE_CHANNEL_ROUTING", "locale", "bool", dependencies=(("locale_resolution", "enforce"), ("template_variants", "enforce"))),
    )
}


def _truthy(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes"}


def platform_mode(spec: FeatureSpec) -> str:
    raw = os.getenv(spec.env, "false" if spec.kind == "bool" else "off").strip().lower()
    if spec.kind == "bool":
        return "enforce" if _truthy(raw) else "off"
    return raw if raw in {"off", "shadow", "enforce"} else "off"


class EngineRolloutService:
    def __init__(self, db: Session):
        self.db = db

    def rows(self, project_id: int) -> dict[str, ProjectEngineRollout]:
        return {row.feature_key: row for row in self.db.query(ProjectEngineRollout).filter(ProjectEngineRollout.project_id == project_id).all()}

    def effective_modes(self, project_id: int, overrides: dict[str, str] | None = None) -> dict[str, str]:
        rows = self.rows(project_id)
        emergency = _truthy(os.getenv("SEND_ENGINE_EMERGENCY_OFF"))
        result = {}
        for key, spec in FEATURES.items():
            row = rows.get(key)
            requested = (overrides or {}).get(key, row.mode if row else "inherit")
            result[key] = "off" if emergency else (platform_mode(spec) if requested == "inherit" else requested)
        return result

    def feature(self, project_id: int, key: str) -> dict[str, Any]:
        spec = FEATURES[key]; row = self.rows(project_id).get(key)
        return {
            "feature_key": key, "group": spec.group, "environment_variable": spec.env,
            "platform_mode": platform_mode(spec), "project_mode": row.mode if row else "inherit",
            "effective_mode": self.effective_modes(project_id)[key], "config": row.config if row else None,
            "version": row.version if row else 0,
            "dependencies": [{"feature_key": dep, "minimum_mode": mode} for dep, mode in spec.dependencies],
        }

    def list_features(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.rows(project_id); effective = self.effective_modes(project_id)
        result = []
        for key, spec in FEATURES.items():
            row = rows.get(key)
            result.append({
                "feature_key": key, "group": spec.group, "environment_variable": spec.env,
                "platform_mode": platform_mode(spec), "project_mode": row.mode if row else "inherit",
                "effective_mode": effective[key], "config": row.config if row else None,
                "version": row.version if row else 0,
                "dependencies": [{"feature_key": dep, "minimum_mode": mode} for dep, mode in spec.dependencies],
            })
        return result

    def validate(self, project_id: int, updates: list[dict[str, Any]]) -> list[dict[str, str]]:
        proposed = {}
        for update in updates:
            key, mode = update["feature_key"], update["mode"]
            if key not in FEATURES: raise ValueError(f"Unknown engine feature: {key}")
            if mode not in MODES: raise ValueError(f"Invalid rollout mode for {key}: {mode}")
            if key in proposed: raise ValueError(f"Duplicate engine feature: {key}")
            self._validate_config(key, update.get("config"))
            proposed[key] = mode
        effective = self.effective_modes(project_id, proposed); errors = []
        for key, spec in FEATURES.items():
            if effective[key] != "enforce": continue
            for dependency, minimum in spec.dependencies:
                if MODE_RANK[effective[dependency]] < MODE_RANK[minimum]:
                    errors.append({"feature_key": key, "dependency": dependency, "minimum_mode": minimum, "effective_mode": effective[dependency]})
        for key in ("base_compile", "bandit_act", "locale_channel_routing", "future_plan"):
            if effective[key] != "shadow":
                continue
            for dependency, _ in FEATURES[key].dependencies:
                if MODE_RANK[effective[dependency]] < MODE_RANK["shadow"]:
                    errors.append({
                        "feature_key": key,
                        "dependency": dependency,
                        "minimum_mode": "shadow",
                        "effective_mode": effective[dependency],
                    })
        if effective["guardian"] == "enforce" and self._quiet_hours_conflict(project_id):
            errors.append({
                "feature_key": "guardian",
                "dependency": "project_policy.quiet_hours",
                "minimum_mode": "canonical",
                "effective_mode": "conflict",
            })
        return errors

    @staticmethod
    def _validate_config(feature_key: str, config: dict[str, Any] | None) -> None:
        if config is None:
            return
        if not isinstance(config, dict):
            raise ValueError(f"Config for {feature_key} must be an object")
        allowed = {
            "candidate": {"ttl_minutes"},
            "future_plan": {
                "horizon_minutes",
                "collision_window_minutes",
                "recheck_minutes",
                "max_candidates_per_contact",
            },
        }.get(feature_key, set())
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(
                f"Unknown config for {feature_key}: " + ", ".join(sorted(unknown))
            )
        if feature_key == "candidate" and "ttl_minutes" in config:
            try:
                ttl = int(config["ttl_minutes"])
            except (TypeError, ValueError) as exc:
                raise ValueError("candidate.ttl_minutes must be an integer") from exc
            if ttl < 1 or ttl > 10080:
                raise ValueError("candidate.ttl_minutes must be between 1 and 10080")
        if feature_key == "future_plan":
            limits = {
                "horizon_minutes": (60, 43200),
                "collision_window_minutes": (1, 10080),
                "recheck_minutes": (1, 1440),
                "max_candidates_per_contact": (2, 1000),
            }
            for field, (minimum, maximum) in limits.items():
                if field not in config:
                    continue
                try:
                    value = int(config[field])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"future_plan.{field} must be an integer") from exc
                if value < minimum or value > maximum:
                    raise ValueError(
                        f"future_plan.{field} must be between {minimum} and {maximum}"
                    )
            if (
                "horizon_minutes" in config
                and "collision_window_minutes" in config
                and int(config["collision_window_minutes"]) > int(config["horizon_minutes"])
            ):
                raise ValueError(
                    "future_plan.collision_window_minutes cannot exceed horizon_minutes"
                )

    def _quiet_hours_conflict(self, project_id: int) -> bool:
        """Fail closed when legacy and canonical quiet-hour settings diverge."""
        from app.models import ProjectPolicy, ProjectSendConfig

        legacy = self.db.query(ProjectSendConfig).filter(
            ProjectSendConfig.project_id == project_id,
        ).first()
        policy = self.db.query(ProjectPolicy).filter(
            ProjectPolicy.project_id == project_id,
        ).first()
        if not legacy or not legacy.quiet_hours_enabled:
            return False
        canonical = (policy.quiet_hours if policy else None) or {}
        expected = {
            "enabled": True,
            "start": legacy.quiet_hours_start,
            "end": legacy.quiet_hours_end,
            "timezone": legacy.quiet_hours_timezone or "UTC",
        }
        return any(canonical.get(key) != value for key, value in expected.items())

    def update(self, project_id: int, updates: list[dict[str, Any]], actor_user_id: int | None) -> list[dict[str, Any]]:
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"engine-rollout:{project_id}"})
        rows = self.rows(project_id)
        # A stale writer must receive a version conflict even when its proposed
        # state would also violate dependencies.  Check this under the same
        # project lock before evaluating the candidate configuration.
        for update in updates:
            key = update["feature_key"]
            row = rows.get(key)
            expected = int(update.get("expected_version", 0))
            if (row and row.version != expected) or (not row and expected != 0):
                raise LookupError(f"Version conflict for {key}")
        errors = self.validate(project_id, updates)
        if errors:
            raise RuntimeError(errors)
        for update in updates:
            key = update["feature_key"]; row = rows.get(key); expected = int(update.get("expected_version", 0))
            previous_mode, previous_config = (row.mode, row.config) if row else (None, None)
            if row is None:
                row = ProjectEngineRollout(project_id=project_id, feature_key=key); self.db.add(row)
            row.mode = update["mode"]
            if "config" in update:
                row.config = update["config"]
            row.updated_by_user_id = actor_user_id
            row.version = int(row.version or 0) + 1
            self.db.add(ProjectEngineRolloutEvent(project_id=project_id, feature_key=key, previous_mode=previous_mode, new_mode=row.mode, previous_config=previous_config, new_config=row.config, actor_user_id=actor_user_id))
        self.db.commit()
        return self.list_features(project_id)


def effective_mode(db: Session | None, project_id: int | None, key: str) -> str:
    if not db or not project_id: return platform_mode(FEATURES[key])
    return EngineRolloutService(db).effective_modes(project_id)[key]
