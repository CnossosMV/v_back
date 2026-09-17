"""Persistent, agent-first campaign recipes and episode planning."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import Project
from app.models.campaigns import CampaignRecipe, CampaignRecipeEpisode, CampaignRun
from app.models.project_import import LifecycleModel
from app.schemas.campaign_recipes import (
    CampaignEpisodeMaterialize,
    CampaignRecipeCreate,
    CampaignRecipeUpdate,
)
from app.schemas.campaigns import CampaignCreate
from app.schemas.commercial_calendar import OpportunityPreviewInput, OpportunityRuleInput
from app.services.campaigns.channel_registry import CampaignChannelRegistry
from app.services.campaigns.commercial_calendar import CommercialCalendarService
from app.services.campaigns.service import CampaignService
from app.services.contact_groups.compiler import compile_group_query


class CampaignRecipeError(ValueError):
    pass


_TERMINAL_EPISODE_STATES = {
    "materialized", "scheduled", "completed", "canceled", "failed", "expired", "superseded",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _bounded_key(value: str, limit: int = 255) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:20]
    return f"{value[: limit - 21]}:{digest}"


class CampaignRecipeService:
    def __init__(self, db: Session, clock=None):
        self.db = db
        from app.services.clock import SystemClock
        self.clock = clock or SystemClock()
        self.calendar = CommercialCalendarService(db)

    @staticmethod
    def contract() -> dict[str, Any]:
        return {
            "version": "1.0",
            "concepts": {
                "base": (
                    "Standing Type/Stage/Age nurture remains available when no episode wins attention."
                ),
                "recipe": (
                    "Persistent tenant policy for audience, market calendar, priority, copy mode and review policy."
                ),
                "episode": (
                    "One dated opportunity produced by a recipe; it expires if nobody authorizes it."
                ),
                "campaign": (
                    "Draft executable definition materialized from one episode. It is still inactive."
                ),
                "run": (
                    "A separately consequence-gated send authorization for an active campaign."
                ),
            },
            "required_tenant_decisions": [
                "stable purpose_key",
                "lifecycle model and rule-based audience",
                "country/region/timezone (locale alone is not a calendar)",
                "recurring opportunity rules and their declared priorities",
                "attention entry/fallthrough policy",
                "content mode and content brief",
                "planning horizon and decision lead time",
            ],
            "content_modes": {
                "fixed": "Tenant-approved actions are reused; automatic materialization creates drafts only.",
                "agent_draft": "The code agent writes each episode from the brief before materialization.",
                "bounded_autonomy": (
                    "The code agent may write within declared facts/claims/tone, but activation stays manual."
                ),
            },
            "state_machine": [
                "preview recipe",
                "create draft recipe with confirmation",
                "activate recipe with confirmation",
                "worker materializes episode planning records",
                "agent reads planning inbox and writes copy when required",
                "materialize draft campaign with confirmation",
                "preview campaign plan and orchestration impact",
                "activate campaign with confirmation",
                "evaluate and choose run consequences",
                "schedule run with confirmation",
                "monitor or cancel the run through MCP; Selection and Guardian re-evaluate at dispatch",
            ],
            "safety_invariants": [
                "recipe activation never activates a campaign",
                "episode materialization never creates a run",
                "the recipe worker never calls a provider",
                "every generated audience is bound to one lifecycle_model_id",
                "unresolved priority collisions block episode materialization",
                "if an episode expires, Base remains eligible",
                "campaign activation and run authorization are separate confirmations",
            ],
            "unattended_behavior": {
                "fixed": "create draft campaigns only when configured automatic",
                "agent_authored": "leave an inbox item awaiting copy",
                "missed_deadline": "expire the episode and keep Base; never send stale copy",
            },
            "external_sends": 0,
        }

    def get(self, project_id: int, recipe_id: int) -> CampaignRecipe | None:
        return self.db.query(CampaignRecipe).filter(
            CampaignRecipe.id == recipe_id,
            CampaignRecipe.project_id == project_id,
        ).first()

    def list(self, project_id: int, include_archived: bool = False) -> list[CampaignRecipe]:
        query = self.db.query(CampaignRecipe).filter(CampaignRecipe.project_id == project_id)
        if not include_archived:
            query = query.filter(CampaignRecipe.status != "archived")
        return query.order_by(CampaignRecipe.updated_at.desc(), CampaignRecipe.id.desc()).all()

    @staticmethod
    def recipe_payload(row: CampaignRecipe) -> dict[str, Any]:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "external_key": row.external_key,
            "name": row.name,
            "description": row.description,
            "status": row.status,
            "purpose_key": row.purpose_key,
            "lifecycle_model_id": row.lifecycle_model_id,
            "timezone": row.timezone,
            "country_code": row.country_code,
            "region_code": row.region_code,
            "default_channel": row.default_channel,
            "selection_config": row.selection_config or {},
            "policy_config": row.policy_config or {},
            "attention_policy": row.attention_policy or {},
            "schedule_rules": row.schedule_rules or [],
            "include_persisted_opportunities": bool(row.include_persisted_opportunities),
            "content_mode": row.content_mode,
            "content_brief": row.content_brief or {},
            "fixed_actions": row.fixed_actions or None,
            "autonomy_policy": row.autonomy_policy or {},
            "planning_horizon_days": row.planning_horizon_days,
            "decision_lead_hours": row.decision_lead_hours,
            "version": row.version,
            "last_materialized_at": _iso(row.last_materialized_at),
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def episode_payload(row: CampaignRecipeEpisode, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.utcnow()
        next_action = {
            "awaiting_copy": "author_copy_then_materialize",
            "ready": "review_then_materialize",
            "blocked": "resolve_priority_collision",
            "materialized": "preview_and_activate_campaign",
            "scheduled": "monitor_campaign_run",
            "completed": "review_outcomes",
            "canceled": "none",
            "failed": "review_failure",
        }.get(row.status, "none")
        return {
            "id": row.id,
            "project_id": row.project_id,
            "recipe_id": row.recipe_id,
            "recipe_version": row.recipe_version,
            "occurrence_key": row.occurrence_key,
            "status": row.status,
            "opportunity": row.opportunity_snapshot or {},
            "collisions": row.collision_snapshot or [],
            "collision_resolution": row.collision_resolution,
            "content_brief": row.content_brief_snapshot or {},
            "decision_due_at": _iso(row.decision_due_at),
            "starts_at": _iso(row.starts_at),
            "target_at": _iso(row.target_at),
            "expires_at": _iso(row.expires_at),
            "campaign_id": row.campaign_id,
            "run_id": row.run_id,
            "materialized_at": _iso(row.materialized_at),
            "overdue": row.status not in _TERMINAL_EPISODE_STATES and row.decision_due_at <= current,
            "next_action": next_action,
            "external_sends": 0,
        }

    def _validate_project_model(self, project_id: int, lifecycle_model_id: int, *, activating: bool = False):
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise CampaignRecipeError("Project not found")
        model = self.db.query(LifecycleModel).filter(
            LifecycleModel.id == lifecycle_model_id,
            LifecycleModel.project_id == project_id,
        ).first()
        if not model or model.status == "archived":
            raise CampaignRecipeError("Lifecycle model is missing or archived")
        allowed = {"shadow", "active"} if activating else {"validated", "shadow", "active"}
        if model.status not in allowed:
            raise CampaignRecipeError(
                f"Lifecycle model status must be one of {sorted(allowed)}; got {model.status!r}"
            )
        return project, model

    @staticmethod
    def _namespaced_rules(payload: CampaignRecipeCreate) -> list[OpportunityRuleInput]:
        rules: list[OpportunityRuleInput] = []
        for rule in payload.schedule_rules:
            values = rule.model_dump(mode="python")
            values["rule_key"] = _bounded_key(f"{payload.external_key}.{rule.rule_key}", 120)
            values["timezone"] = rule.timezone or payload.timezone
            values["country_code"] = rule.country_code or payload.country_code
            values["region_code"] = rule.region_code or payload.region_code
            values["purpose_keys"] = sorted(set(rule.purpose_keys) | {payload.purpose_key})
            rules.append(OpportunityRuleInput(**values))
        return rules

    def _definition_from_row(self, row: CampaignRecipe) -> CampaignRecipeCreate:
        values = self.recipe_payload(row)
        values["status"] = "draft"
        for key in ("id", "project_id", "version", "last_materialized_at", "created_at", "updated_at"):
            values.pop(key, None)
        return CampaignRecipeCreate(**values)

    def preview(
        self,
        project_id: int,
        payload: CampaignRecipeCreate,
        *,
        exclude_recipe_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or self.clock.utcnow()
        _project, model = self._validate_project_model(
            project_id, payload.lifecycle_model_id, activating=False,
        )
        if CampaignChannelRegistry.get(payload.default_channel) is None:
            raise CampaignRecipeError(f"Campaign channel is not registered: {payload.default_channel}")
        compile_group_query(
            self.db,
            project_id,
            payload.selection_config.get("rule_config"),
            now=current,
            lifecycle_model_id=payload.lifecycle_model_id,
        ).limit(1).statement
        horizon_end = current + timedelta(days=payload.planning_horizon_days)
        calendar = self.calendar.preview(project_id, OpportunityPreviewInput(
            horizon_start=current,
            horizon_end=horizon_end,
            rules=self._namespaced_rules(payload),
            include_persisted=payload.include_persisted_opportunities,
            include_drafts=False,
            purpose_key=payload.purpose_key,
            country_code=payload.country_code,
            region_code=payload.region_code,
        ))
        own_occurrences: list[dict[str, Any]] = []
        for source in calendar["occurrences"]:
            item = dict(source)
            calendar_key = str(item["external_key"])
            item["calendar_external_key"] = calendar_key
            item["external_key"] = _bounded_key(f"{payload.external_key}:{calendar_key}")
            item["recipe_external_key"] = payload.external_key
            own_occurrences.append(item)

        existing_query = self.db.query(CampaignRecipeEpisode).filter(
            CampaignRecipeEpisode.project_id == project_id,
            CampaignRecipeEpisode.status.in_(["awaiting_copy", "ready", "blocked", "materialized", "scheduled"]),
            CampaignRecipeEpisode.expires_at >= current,
            CampaignRecipeEpisode.starts_at <= horizon_end,
        )
        if exclude_recipe_id is not None:
            existing_query = existing_query.filter(CampaignRecipeEpisode.recipe_id != exclude_recipe_id)
        existing_occurrences = [dict(row.opportunity_snapshot or {}) for row in existing_query.all()]
        all_occurrences = sorted(
            [*own_occurrences, *existing_occurrences],
            key=lambda item: (item["starts_at"], -int(item.get("priority") or 0), item["external_key"]),
        )
        overlaps = self.calendar._overlaps(all_occurrences)
        own_keys = {item["external_key"] for item in own_occurrences}
        relevant_overlaps = [
            item for item in overlaps
            if any(key in own_keys for key in item["opportunity_keys"])
        ]
        collision_map: dict[str, list[dict[str, Any]]] = {key: [] for key in own_keys}
        for overlap in relevant_overlaps:
            for key in overlap["opportunity_keys"]:
                if key in collision_map:
                    collision_map[key].append(overlap)
        occurrences = []
        for item in own_occurrences:
            collisions = collision_map[item["external_key"]]
            occurrences.append({
                **item,
                "collisions": collisions,
                "blocked": any(value.get("resolution_required") for value in collisions),
                "decision_due_at": (
                    datetime.fromisoformat(item["starts_at"]) - timedelta(hours=payload.decision_lead_hours)
                ).isoformat(),
            })
        unresolved = [item for item in relevant_overlaps if item.get("resolution_required")]
        return {
            "project_id": project_id,
            "lifecycle_model": {
                "id": model.id,
                "version": model.version,
                "name": model.name,
                "status": model.status,
                "checksum": model.checksum,
            },
            "horizon_start": current.isoformat(),
            "horizon_end": horizon_end.isoformat(),
            "occurrences": occurrences,
            "occurrence_count": len(occurrences),
            "overlaps": relevant_overlaps,
            "unresolved_collisions": unresolved,
            "unresolved_collision_count": len(unresolved),
            "can_activate": model.status in {"shadow", "active"} and not unresolved,
            "copy_workload": {
                "mode": payload.content_mode,
                "episodes_requiring_agent_copy": (
                    0 if payload.content_mode == "fixed" else len(occurrences)
                ),
                "fixed_drafts_eligible_for_automatic_materialization": (
                    len(occurrences)
                    if payload.content_mode == "fixed"
                    and payload.autonomy_policy.episode_materialization == "automatic"
                    else 0
                ),
            },
            "consequence_at_gate": {
                "creates_recipe": False,
                "creates_episode_records": False,
                "creates_campaign_drafts": False,
                "activates_campaigns": False,
                "creates_runs": False,
                "external_sends": 0,
                "unattended_behavior": payload.autonomy_policy.unattended_behavior,
            },
            "next": (
                "Resolve unresolved collisions before activation."
                if unresolved
                else "Create the recipe as draft, review it, then activate it explicitly."
            ),
        }

    def create(
        self,
        project_id: int,
        payload: CampaignRecipeCreate,
        actor_user_id: int | None,
        *,
        commit: bool = True,
    ) -> CampaignRecipe:
        self.preview(project_id, payload)
        existing = self.db.query(CampaignRecipe.id).filter(
            CampaignRecipe.project_id == project_id,
            CampaignRecipe.external_key == payload.external_key,
        ).first()
        if existing:
            raise CampaignRecipeError("Campaign recipe external_key already exists")
        values = payload.model_dump(mode="json")
        values["attention_policy"] = values.get("attention_policy") or {}
        row = CampaignRecipe(
            project_id=project_id,
            created_by_user_id=actor_user_id,
            **values,
        )
        self.db.add(row)
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(row)
        return row

    def update(
        self,
        row: CampaignRecipe,
        changes: CampaignRecipeUpdate,
        *,
        commit: bool = True,
    ) -> CampaignRecipe:
        if row.status not in {"draft", "paused"}:
            raise CampaignRecipeError("Pause an active recipe before editing it")
        merged = self.recipe_payload(row)
        for key in ("id", "project_id", "version", "last_materialized_at", "created_at", "updated_at"):
            merged.pop(key, None)
        merged["status"] = "draft"
        merged.update(changes.model_dump(mode="json", exclude_unset=True))
        payload = CampaignRecipeCreate(**merged)
        self.preview(row.project_id, payload, exclude_recipe_id=row.id)
        values = payload.model_dump(mode="json")
        values.pop("status", None)
        for field, value in values.items():
            setattr(row, field, value)
        row.version = int(row.version or 1) + 1
        row.updated_at = self.clock.utcnow()
        self.db.query(CampaignRecipeEpisode).filter(
            CampaignRecipeEpisode.recipe_id == row.id,
            CampaignRecipeEpisode.status.in_(["awaiting_copy", "ready", "blocked"]),
        ).update({"status": "superseded"}, synchronize_session=False)
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(row)
        return row

    def set_state(
        self,
        row: CampaignRecipe,
        target_status: str,
        *,
        commit: bool = True,
    ) -> tuple[CampaignRecipe, dict[str, Any]]:
        if target_status not in {"active", "paused", "archived"}:
            raise CampaignRecipeError("target_status must be active, paused or archived")
        if row.status == "archived":
            raise CampaignRecipeError("Archived recipes are immutable")
        if target_status == "active":
            preview = self.preview(
                row.project_id,
                self._definition_from_row(row),
                exclude_recipe_id=row.id,
            )
            if not preview["can_activate"]:
                raise CampaignRecipeError("Recipe activation is blocked by model state or unresolved collisions")
        else:
            preview = {
                "can_activate": None,
                "state_change": f"{row.status}->{target_status}",
                "note": "Stopping recipe planning does not require activation validation.",
                "external_sends": 0,
            }
        row.status = target_status
        row.version = int(row.version or 1) + 1
        row.updated_at = self.clock.utcnow()
        self.db.flush()
        if target_status == "active":
            self.sync_recipe(row, commit=False)
        if commit:
            self.db.commit()
            self.db.refresh(row)
        return row, preview

    def sync_recipe(
        self,
        row: CampaignRecipe,
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> dict[str, int]:
        if row.status != "active":
            return {"created": 0, "updated": 0, "drafts_materialized": 0, "expired": 0}
        current = now or self.clock.utcnow()
        preview = self.preview(
            row.project_id,
            self._definition_from_row(row),
            exclude_recipe_id=row.id,
            now=current,
        )
        created = updated = drafts = 0
        for occurrence in preview["occurrences"]:
            occurrence_key = occurrence["external_key"]
            existing = self.db.query(CampaignRecipeEpisode).filter(
                CampaignRecipeEpisode.recipe_id == row.id,
                CampaignRecipeEpisode.occurrence_key == occurrence_key,
            ).first()
            starts_at = datetime.fromisoformat(occurrence["starts_at"])
            expires_at = datetime.fromisoformat(occurrence["expires_at"])
            target_at = datetime.fromisoformat(occurrence["peak_at"]) if occurrence.get("peak_at") else starts_at
            collisions = occurrence.get("collisions") or []
            next_status = "blocked" if occurrence.get("blocked") else (
                "ready" if row.content_mode == "fixed" else "awaiting_copy"
            )
            snapshot = {key: value for key, value in occurrence.items() if key not in {"collisions", "blocked", "decision_due_at"}}
            if existing is None:
                existing = CampaignRecipeEpisode(
                    project_id=row.project_id,
                    recipe_id=row.id,
                    recipe_version=row.version,
                    occurrence_key=occurrence_key,
                    status=next_status,
                    opportunity_snapshot=snapshot,
                    collision_snapshot=collisions,
                    content_brief_snapshot=row.content_brief or {},
                    decision_due_at=starts_at - timedelta(hours=row.decision_lead_hours),
                    starts_at=starts_at,
                    target_at=target_at,
                    expires_at=expires_at,
                )
                self.db.add(existing)
                self.db.flush()
                created += 1
            elif existing.status not in _TERMINAL_EPISODE_STATES:
                existing.recipe_version = row.version
                existing.status = next_status
                existing.opportunity_snapshot = snapshot
                existing.collision_snapshot = collisions
                existing.content_brief_snapshot = row.content_brief or {}
                existing.decision_due_at = starts_at - timedelta(hours=row.decision_lead_hours)
                existing.starts_at = starts_at
                existing.target_at = target_at
                existing.expires_at = expires_at
                updated += 1
            if (
                existing.status == "ready"
                and row.content_mode == "fixed"
                and (row.autonomy_policy or {}).get("episode_materialization") == "automatic"
            ):
                self.materialize_episode(existing, None, actor_user_id=None, commit=False)
                drafts += 1
        expired = self.db.query(CampaignRecipeEpisode).filter(
            CampaignRecipeEpisode.recipe_id == row.id,
            CampaignRecipeEpisode.expires_at <= current,
            CampaignRecipeEpisode.status.in_(["awaiting_copy", "ready", "blocked"]),
        ).update({"status": "expired"}, synchronize_session=False)
        row.last_materialized_at = current
        self.db.flush()
        if commit:
            self.db.commit()
        return {
            "created": created,
            "updated": updated,
            "drafts_materialized": drafts,
            "expired": int(expired or 0),
        }

    def sync_all(self, *, now: datetime | None = None) -> dict[str, int]:
        totals = {
            "recipes": 0, "created": 0, "updated": 0,
            "drafts_materialized": 0, "expired": 0, "runs_reconciled": 0,
        }
        totals["runs_reconciled"] = self._reconcile_scheduled_episodes()
        rows = self.db.query(CampaignRecipe).filter(CampaignRecipe.status == "active").all()
        for row in rows:
            result = self.sync_recipe(row, now=now, commit=False)
            totals["recipes"] += 1
            for key in ("created", "updated", "drafts_materialized", "expired"):
                totals[key] += result[key]
        self.db.commit()
        return totals

    def _reconcile_scheduled_episodes(self, project_id: int | None = None) -> int:
        query = self.db.query(CampaignRecipeEpisode, CampaignRun).join(
            CampaignRun,
            CampaignRun.id == CampaignRecipeEpisode.run_id,
        ).filter(CampaignRecipeEpisode.status == "scheduled")
        if project_id is not None:
            query = query.filter(CampaignRecipeEpisode.project_id == project_id)
        changed = 0
        for episode, run in query.all():
            terminal = {
                "completed": "completed",
                "canceled": "canceled",
                "failed": "failed",
            }.get(run.status)
            if terminal:
                episode.status = terminal
                changed += 1
        if changed:
            self.db.flush()
        return changed

    def planning_inbox(
        self,
        project_id: int,
        *,
        include_terminal: bool = False,
        limit: int = 100,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or self.clock.utcnow()
        self._reconcile_scheduled_episodes(project_id)
        query = self.db.query(CampaignRecipeEpisode).filter(
            CampaignRecipeEpisode.project_id == project_id,
        )
        if not include_terminal:
            query = query.filter(CampaignRecipeEpisode.status.in_([
                "awaiting_copy", "ready", "blocked", "materialized", "scheduled",
            ]))
        rows = query.order_by(
            CampaignRecipeEpisode.decision_due_at,
            CampaignRecipeEpisode.starts_at,
            CampaignRecipeEpisode.id,
        ).limit(min(max(limit, 1), 250)).all()
        recipe_ids = {row.recipe_id for row in rows}
        recipes = {
            row.id: row for row in self.db.query(CampaignRecipe).filter(
                CampaignRecipe.id.in_(recipe_ids)
            ).all()
        } if recipe_ids else {}
        items = []
        for row in rows:
            payload = self.episode_payload(row, now=current)
            recipe = recipes.get(row.recipe_id)
            payload["recipe"] = {
                "id": recipe.id,
                "external_key": recipe.external_key,
                "name": recipe.name,
                "purpose_key": recipe.purpose_key,
                "content_mode": recipe.content_mode,
                "lifecycle_model_id": recipe.lifecycle_model_id,
            } if recipe else None
            items.append(payload)
        return {
            "project_id": project_id,
            "as_of": current.isoformat(),
            "items": items,
            "count": len(items),
            "summary": {
                state: sum(1 for item in items if item["status"] == state)
                for state in ("blocked", "awaiting_copy", "ready", "materialized", "scheduled")
            },
            "instructions": [
                "Resolve blocked items by changing declared recipe priorities; never invent a winner.",
                "For awaiting_copy, write only from content_brief.source_facts and declared claims.",
                "Materialize creates a draft campaign only.",
                "Preview plan and orchestration impact before activation.",
                "Evaluate campaign-run consequences before scheduling a run.",
            ],
            "external_sends": 0,
        }

    def get_episode(self, project_id: int, episode_id: int) -> CampaignRecipeEpisode | None:
        return self.db.query(CampaignRecipeEpisode).filter(
            CampaignRecipeEpisode.id == episode_id,
            CampaignRecipeEpisode.project_id == project_id,
        ).first()

    def materialize_episode(
        self,
        episode: CampaignRecipeEpisode,
        request: CampaignEpisodeMaterialize | None,
        actor_user_id: int | None,
        *,
        commit: bool = True,
    ) -> CampaignRecipeEpisode:
        if episode.campaign_id:
            return episode
        if episode.status == "blocked":
            raise CampaignRecipeError("Resolve the episode's priority collision before materialization")
        if episode.status not in {"awaiting_copy", "ready"}:
            raise CampaignRecipeError(f"Episode status {episode.status!r} cannot be materialized")
        current = self.clock.utcnow()
        if episode.expires_at <= current:
            episode.status = "expired"
            self.db.flush()
            raise CampaignRecipeError("Episode expired; Base remains eligible")
        recipe = self.get(episode.project_id, episode.recipe_id)
        if not recipe or recipe.status != "active":
            raise CampaignRecipeError("Campaign recipe must be active")
        actions = recipe.fixed_actions if recipe.content_mode == "fixed" else (
            request.model_dump(mode="json").get("actions") if request else None
        )
        if not actions:
            raise CampaignRecipeError("This episode requires agent-authored actions")
        opportunity = episode.opportunity_snapshot or {}
        duration_hours = max(1, int((episode.expires_at - episode.starts_at).total_seconds() / 3600))
        campaign_values = {
            "name": f"{recipe.name} · {episode.starts_at.date().isoformat()}",
            "description": (
                f"Episode {episode.occurrence_key} materialized from recipe {recipe.external_key}."
            ),
            "campaign_type": "one_off",
            "status": "draft",
            "default_channel": recipe.default_channel,
            "selection_config": {
                **(recipe.selection_config or {}),
                "lifecycle_model_id": recipe.lifecycle_model_id,
            },
            "policy_config": recipe.policy_config or {},
            "opportunity_config": {
                "opportunity_type": opportunity.get("opportunity_type") or "custom",
                "priority": int(opportunity.get("priority") or 0),
                "priority_source": opportunity.get("priority_source") or "tie_requires_decision",
                "priority_reason": opportunity.get("priority_reason"),
                "decision_lead_hours": recipe.decision_lead_hours,
                "attention_window_hours": min(duration_hours, 24 * 31),
                "calendar_external_key": opportunity.get("calendar_external_key"),
                "combine_with": sorted({
                    key
                    for collision in (episode.collision_snapshot or [])
                    for key in collision.get("opportunity_keys", [])
                    if key != episode.occurrence_key
                }),
                "context": {
                    **(opportunity.get("context") or {}),
                    "campaign_recipe_id": recipe.id,
                    "campaign_recipe_episode_id": episode.id,
                    "content_mode": recipe.content_mode,
                },
            },
            "timezone": recipe.timezone,
            "starts_at": episode.starts_at,
            "target_at": episode.target_at,
            "external_key": _bounded_key(f"campaign-recipe:{recipe.external_key}:{episode.occurrence_key}"),
            "purpose_key": recipe.purpose_key,
            "attention_policy": recipe.attention_policy or {},
            "actions": actions,
        }
        validated = CampaignCreate(**campaign_values)
        campaign = CampaignService(self.db).create(
            episode.project_id,
            validated.model_dump(mode="python", exclude_unset=True),
            actor_user_id,
            commit=False,
        )
        episode.campaign_id = campaign.id
        episode.authored_by_user_id = actor_user_id
        episode.materialized_at = current
        episode.status = "materialized"
        if request and request.copy_rationale:
            episode.content_brief_snapshot = {
                **(episode.content_brief_snapshot or {}),
                "copy_rationale": request.copy_rationale,
            }
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(episode)
        return episode
