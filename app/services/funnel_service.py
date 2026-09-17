"""CRUD service for the Funnel Engine."""

from collections import deque
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from fastapi import HTTPException, status
from typing import List, Optional, Set

from app.models import (
    Funnel, FunnelStep, FunnelEnrollment, Project, User,
    WhatsAppInstance, Chatbot, AgentTeam, ProjectApiConnection,
)
from app.models.messaging import MessagingTemplate
from app.services.email_instance_resolver import resolve_email_instance


# Reference extraction paths: (ref_type, step_config_key, condition_fn_or_None)
REFERENCE_PATHS = [
    # instance_id in send_message / send_whatsapp steps
    ("whatsapp_instance", "instance_id", None),
    # template_id in action steps
    ("email_template", "template_id", None),
    # handoff_id when handoff_type == chatbot
    ("chatbot", "handoff_id", lambda cfg: cfg.get("handoff_type") == "chatbot"),
    # handoff_id when handoff_type == agent_team
    ("agent_team", "handoff_id", lambda cfg: cfg.get("handoff_type") == "agent_team"),
    # on_topic_handler.id when type == chatbot (in wait_for_reply)
    ("chatbot", "on_topic_handler", lambda cfg: isinstance(cfg.get("on_topic_handler"), dict) and cfg["on_topic_handler"].get("type") == "chatbot"),
    # on_topic_handler.id when type == agent_team
    ("agent_team", "on_topic_handler", lambda cfg: isinstance(cfg.get("on_topic_handler"), dict) and cfg["on_topic_handler"].get("type") == "agent_team"),
    # connection_id in action steps with action_type == api_call
    ("api_connection", "connection_id", lambda cfg: cfg.get("action_type") == "api_call"),
    # email instance_id in send_message steps with channel == email
    ("email_instance", "email_instance_id", lambda cfg: cfg.get("channel") == "email"),
]


class FunnelService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_project(self, project_id: int, workspace_id: int) -> Project:
        project = self.db.query(Project).filter(
            Project.id == project_id,
            Project.workspace_id == workspace_id,
            Project.is_active == True,
        ).first()
        if not project:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found or access denied")
        return project

    def _get_funnel(self, funnel_id: int, workspace_id: int) -> Funnel:
        funnel = (
            self.db.query(Funnel)
            .join(Project)
            .filter(
                Funnel.id == funnel_id,
                Project.workspace_id == workspace_id,
                Project.is_active == True,
            )
            .first()
        )
        if not funnel:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Funnel not found")
        return funnel

    @staticmethod
    def _require_editable_funnel(funnel: Funnel) -> None:
        if funnel.status == "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pause the funnel before changing its active definition",
            )

    def _counts(self, funnel_id: int):
        step_count = self.db.query(func.count(FunnelStep.id)).filter(FunnelStep.funnel_id == funnel_id).scalar() or 0
        enrollment_count = self.db.query(func.count(FunnelEnrollment.id)).filter(FunnelEnrollment.funnel_id == funnel_id).scalar() or 0
        return step_count, enrollment_count

    def _get_all_descendants(self, step_id: int, funnel_id: int) -> List[FunnelStep]:
        """BFS collection of all descendant steps."""
        result: List[FunnelStep] = []
        queue = deque([step_id])
        while queue:
            pid = queue.popleft()
            children = (
                self.db.query(FunnelStep)
                .filter(FunnelStep.funnel_id == funnel_id, FunnelStep.parent_step_id == pid)
                .all()
            )
            for child in children:
                result.append(child)
                queue.append(child.id)
        return result

    def _get_descendant_ids(self, step_id: int, funnel_id: int) -> Set[int]:
        """Return set of all descendant step IDs (for cycle detection)."""
        ids: Set[int] = set()
        queue = deque([step_id])
        while queue:
            pid = queue.popleft()
            children = (
                self.db.query(FunnelStep.id)
                .filter(FunnelStep.funnel_id == funnel_id, FunnelStep.parent_step_id == pid)
                .all()
            )
            for (cid,) in children:
                ids.add(cid)
                queue.append(cid)
        return ids

    def _close_position_gap(self, funnel_id: int, parent_step_id: Optional[int], branch: str, deleted_position: int):
        """Shift siblings down to fill the gap left by a deleted step."""
        siblings = (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == funnel_id,
                FunnelStep.parent_step_id == parent_step_id,
                FunnelStep.branch == branch,
                FunnelStep.position > deleted_position,
            )
            .all()
        )
        for s in siblings:
            s.position -= 1

    def _open_position_gap(self, funnel_id: int, parent_step_id: Optional[int], branch: str, at_position: int):
        """Shift siblings up to make room at the given position."""
        siblings = (
            self.db.query(FunnelStep)
            .filter(
                FunnelStep.funnel_id == funnel_id,
                FunnelStep.parent_step_id == parent_step_id,
                FunnelStep.branch == branch,
                FunnelStep.position >= at_position,
            )
            .all()
        )
        for s in siblings:
            s.position += 1

    @staticmethod
    def validate_step_config(step_type: str, config: dict | None) -> None:
        """Validate deterministic dynamic timestamp waits at authoring time."""
        config = config or {}
        if step_type != "wait" or config.get("wait_type") != "until_timestamp":
            return
        source = config.get("source")
        path = str(config.get("path") or "")
        if source not in {"contact", "enrollment"}:
            raise HTTPException(status_code=400, detail="until_timestamp source must be contact or enrollment")
        if not path or any(not part or not part.replace("_", "").isalnum() for part in path.split(".")):
            raise HTTPException(status_code=400, detail="until_timestamp path must be a safe dotted JSON path")
        contact_roots = {"properties", "first_seen_at", "last_seen_at", "created_at", "updated_at"}
        enrollment_roots = {"enrollment_metadata", "enrolled_at", "entered_step_at"}
        roots = contact_roots if source == "contact" else enrollment_roots
        if path.split(".", 1)[0] not in roots:
            raise HTTPException(status_code=400, detail=f"until_timestamp path is not allowed for source {source}")
        if config.get("on_missing", "hold") not in {"hold", "advance", "error"}:
            raise HTTPException(status_code=400, detail="until_timestamp on_missing must be hold, advance, or error")
        offset = config.get("offset_seconds", 0)
        if not isinstance(offset, int) or abs(offset) > 315360000:
            raise HTTPException(status_code=400, detail="until_timestamp offset_seconds must be an integer within ten years")

    # ------------------------------------------------------------------
    # Funnel CRUD
    # ------------------------------------------------------------------

    def list_funnels(self, project_id: int, workspace_id: int, include_system: bool = False) -> List[dict]:
        self._get_project(project_id, workspace_id)
        query = self.db.query(Funnel).filter(Funnel.project_id == project_id)
        if not include_system:
            query = query.filter(Funnel.is_system == False)
        funnels = query.order_by(Funnel.created_at.desc()).all()
        results = []
        for f in funnels:
            sc, ec = self._counts(f.id)
            results.append({
                **{c.name: getattr(f, c.name) for c in f.__table__.columns},
                "step_count": sc,
                "enrollment_count": ec,
            })
        return results

    def create_funnel(self, project_id: int, workspace_id: int, user_id: int, data: dict) -> dict:
        self._get_project(project_id, workspace_id)
        funnel = Funnel(
            project_id=project_id,
            name=data["name"],
            description=data.get("description"),
            trigger_type=data["trigger_type"],
            trigger_config=data.get("trigger_config", {}),
            global_exit_config=data.get("global_exit_config", {}),
            purpose_key=data.get("purpose_key"),
            attention_policy=data.get("attention_policy"),
            created_by=user_id,
        )
        self.db.add(funnel)
        self.db.commit()
        self.db.refresh(funnel)
        return self._funnel_response(funnel)

    def get_funnel(self, funnel_id: int, workspace_id: int) -> dict:
        funnel = self._get_funnel(funnel_id, workspace_id)
        return self._funnel_response(funnel)

    def update_funnel(self, funnel_id: int, workspace_id: int, data: dict) -> dict:
        funnel = self._get_funnel(funnel_id, workspace_id)
        if funnel.status == "active" and data:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pause the funnel before changing its active definition",
            )
        for field, value in data.items():
            if value is not None and hasattr(funnel, field):
                setattr(funnel, field, value)
        self.db.commit()
        self.db.refresh(funnel)
        return self._funnel_response(funnel)

    def delete_funnel(self, funnel_id: int, workspace_id: int) -> None:
        funnel = self._get_funnel(funnel_id, workspace_id)
        self.db.delete(funnel)
        self.db.commit()

    def activate_funnel(
        self,
        funnel_id: int,
        workspace_id: int,
        impact_fingerprint: Optional[str],
    ) -> dict:
        funnel = self._get_funnel(funnel_id, workspace_id)
        # Block activation if there are unresolved references
        refs = self.check_references(funnel_id, workspace_id)
        unresolved = [r for r in refs if not r["resolved"]]
        if unresolved:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot activate funnel with unresolved references. Please resolve all references first.",
            )
        from app.services.orchestration_impact_service import (
            OrchestrationImpactError,
            OrchestrationImpactService,
        )
        try:
            OrchestrationImpactService(self.db).ensure_approved(
                "funnel", funnel, impact_fingerprint,
            )
        except OrchestrationImpactError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "orchestration_impact_blocked",
                    "message": "Resolve attention impacts before activating this funnel.",
                    "impact": exc.report,
                },
            ) from exc
        funnel.status = "active"
        self.db.commit()
        self.db.refresh(funnel)
        return self._funnel_response(funnel)

    def pause_funnel(self, funnel_id: int, workspace_id: int) -> dict:
        funnel = self._get_funnel(funnel_id, workspace_id)
        funnel.status = "paused"
        self.db.commit()
        self.db.refresh(funnel)
        return self._funnel_response(funnel)

    # ------------------------------------------------------------------
    # Step CRUD
    # ------------------------------------------------------------------

    def list_steps(self, funnel_id: int, workspace_id: int) -> List[FunnelStep]:
        self._get_funnel(funnel_id, workspace_id)
        return (
            self.db.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel_id)
            .order_by(FunnelStep.position)
            .all()
        )

    def create_step(self, funnel_id: int, workspace_id: int, data: dict) -> FunnelStep:
        funnel = self._get_funnel(funnel_id, workspace_id)
        self._require_editable_funnel(funnel)
        self.validate_step_config(data["step_type"], data.get("step_config"))

        branch = data.get("branch", "main")
        parent_step_id = data.get("parent_step_id")

        # Auto-position at end if not specified
        if data.get("position") is None:
            max_pos = (
                self.db.query(func.max(FunnelStep.position))
                .filter(FunnelStep.funnel_id == funnel_id)
                .scalar()
            )
            data["position"] = (max_pos or 0) + 1
        else:
            # Shift existing steps at or after this position to make room
            siblings = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == funnel_id,
                    FunnelStep.branch == branch,
                    FunnelStep.parent_step_id == parent_step_id,
                    FunnelStep.position >= data["position"],
                )
                .all()
            )
            for s in siblings:
                s.position += 1

        step = FunnelStep(
            funnel_id=funnel_id,
            step_type=data["step_type"],
            step_config=data.get("step_config", {}),
            position=data["position"],
            parent_step_id=parent_step_id,
            branch=branch,
        )
        self.db.add(step)

        # Adopt siblings below into a branch of the new step
        adopt_branch = data.get("adopt_branch")
        if adopt_branch:
            self.db.flush()
            parent_filter = (
                FunnelStep.parent_step_id.is_(None)
                if parent_step_id is None
                else FunnelStep.parent_step_id == parent_step_id
            )
            siblings_to_adopt = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == funnel_id,
                    parent_filter,
                    FunnelStep.branch == branch,
                    FunnelStep.position > step.position,
                    FunnelStep.id != step.id,
                )
                .order_by(FunnelStep.position)
                .all()
            )
            for i, s in enumerate(siblings_to_adopt):
                s.parent_step_id = step.id
                s.branch = adopt_branch
                s.position = i + 1

        self.db.commit()
        self.db.refresh(step)
        return step

    def update_step(self, funnel_id: int, step_id: int, workspace_id: int, data: dict) -> FunnelStep:
        funnel = self._get_funnel(funnel_id, workspace_id)
        self._require_editable_funnel(funnel)
        step = self.db.query(FunnelStep).filter(
            FunnelStep.id == step_id,
            FunnelStep.funnel_id == funnel_id,
        ).first()
        if not step:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

        self.validate_step_config(
            data.get("step_type", step.step_type),
            data.get("step_config", step.step_config),
        )

        nullable_fields = {"parent_step_id"}
        for field, value in data.items():
            if not hasattr(step, field):
                continue
            if value is None and field not in nullable_fields:
                continue
            setattr(step, field, value)
        self.db.commit()
        self.db.refresh(step)
        return step

    def delete_step(self, funnel_id: int, step_id: int, workspace_id: int, promote_branch: Optional[str] = None) -> None:
        funnel = self._get_funnel(funnel_id, workspace_id)
        self._require_editable_funnel(funnel)
        step = self.db.query(FunnelStep).filter(
            FunnelStep.id == step_id,
            FunnelStep.funnel_id == funnel_id,
        ).first()
        if not step:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

        deleted_parent = step.parent_step_id
        deleted_branch = step.branch
        deleted_position = step.position

        if promote_branch:
            # Get children on the promoted branch, sorted by position
            promoted_children = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == funnel_id,
                    FunnelStep.parent_step_id == step_id,
                    FunnelStep.branch == promote_branch,
                )
                .order_by(FunnelStep.position)
                .all()
            )

            # Delete all descendants from OTHER branches (leaf-first)
            other_children = (
                self.db.query(FunnelStep)
                .filter(
                    FunnelStep.funnel_id == funnel_id,
                    FunnelStep.parent_step_id == step_id,
                    FunnelStep.branch != promote_branch,
                )
                .all()
            )
            # Collect all descendants of other-branch children + the children themselves
            to_delete: List[FunnelStep] = []
            for oc in other_children:
                to_delete.extend(self._get_all_descendants(oc.id, funnel_id))
                to_delete.append(oc)
            # Delete leaf-first (reverse BFS order)
            for d in reversed(to_delete):
                self.db.delete(d)

            # Make room for promoted children at the deleted step's position
            num_promoted = len(promoted_children)
            if num_promoted > 0:
                # Shift siblings after deleted position to make room for (num_promoted - 1) extra slots
                # The deleted step occupies 1 slot; promoted children will take num_promoted slots
                if num_promoted > 1:
                    siblings_after = (
                        self.db.query(FunnelStep)
                        .filter(
                            FunnelStep.funnel_id == funnel_id,
                            FunnelStep.parent_step_id == deleted_parent,
                            FunnelStep.branch == deleted_branch,
                            FunnelStep.position > deleted_position,
                            FunnelStep.id != step_id,
                        )
                        .all()
                    )
                    for s in siblings_after:
                        s.position += (num_promoted - 1)

                # Reparent promoted children
                for i, child in enumerate(promoted_children):
                    child.parent_step_id = deleted_parent
                    child.branch = deleted_branch
                    child.position = deleted_position + i
        else:
            # Delete ALL descendants (leaf-first)
            descendants = self._get_all_descendants(step_id, funnel_id)
            for d in reversed(descendants):
                self.db.delete(d)

        # Delete the step itself
        self.db.delete(step)

        # Close gap if no promotion happened, or adjust for promotion
        if not promote_branch:
            self._close_position_gap(funnel_id, deleted_parent, deleted_branch, deleted_position)

        self.db.commit()

    def reorder_steps(self, funnel_id: int, workspace_id: int, items: List[dict]) -> List[FunnelStep]:
        funnel = self._get_funnel(funnel_id, workspace_id)
        self._require_editable_funnel(funnel)
        for item in items:
            step = self.db.query(FunnelStep).filter(
                FunnelStep.id == item["id"],
                FunnelStep.funnel_id == funnel_id,
            ).first()
            if step:
                step.position = item["position"]
                if "parent_step_id" in item:
                    step.parent_step_id = item["parent_step_id"]
                if "branch" in item and item["branch"] is not None:
                    step.branch = item["branch"]
        self.db.commit()
        return self.list_steps(funnel_id, workspace_id)

    def move_step(self, funnel_id: int, step_id: int, workspace_id: int,
                  target_parent_step_id: Optional[int], target_branch: str, target_position: int) -> List[FunnelStep]:
        """Move a step (and its subtree) to a new location."""
        funnel = self._get_funnel(funnel_id, workspace_id)
        self._require_editable_funnel(funnel)
        step = self.db.query(FunnelStep).filter(
            FunnelStep.id == step_id,
            FunnelStep.funnel_id == funnel_id,
        ).first()
        if not step:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

        # Validate target parent exists and is in same funnel
        if target_parent_step_id is not None:
            target_parent = self.db.query(FunnelStep).filter(
                FunnelStep.id == target_parent_step_id,
                FunnelStep.funnel_id == funnel_id,
            ).first()
            if not target_parent:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Target parent step not found")
            # Cycle prevention: target cannot be in step's subtree
            descendant_ids = self._get_descendant_ids(step_id, funnel_id)
            if target_parent_step_id in descendant_ids:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot move step into its own subtree")

        old_parent = step.parent_step_id
        old_branch = step.branch
        old_position = step.position

        # Close gap at old position
        self._close_position_gap(funnel_id, old_parent, old_branch, old_position)

        # If moving within the same parent+branch, adjust target position for the gap we just closed
        if target_parent_step_id == old_parent and target_branch == old_branch:
            if target_position > old_position:
                target_position -= 1

        # Open gap at new position
        self._open_position_gap(funnel_id, target_parent_step_id, target_branch, target_position)

        # Update the step
        step.parent_step_id = target_parent_step_id
        step.branch = target_branch
        step.position = target_position

        self.db.commit()
        return self.list_steps(funnel_id, workspace_id)

    # ------------------------------------------------------------------
    # Response builder
    # ------------------------------------------------------------------

    def restore_steps(self, funnel_id: int, workspace_id: int, snapshot: List[dict]) -> dict:
        """Replace all steps in a funnel with a snapshot (for undo/redo)."""
        funnel = self._get_funnel(funnel_id, workspace_id)

        # Clear parent refs first to avoid FK violations, then delete all
        current = self.db.query(FunnelStep).filter(FunnelStep.funnel_id == funnel_id).all()
        for s in current:
            s.parent_step_id = None
        self.db.flush()
        for s in current:
            self.db.delete(s)
        self.db.flush()

        # Recreate from snapshot, mapping old IDs to new IDs
        id_map: dict = {}
        remaining = list(snapshot)
        max_iter = len(remaining) + 1
        while remaining and max_iter > 0:
            max_iter -= 1
            batch = [s for s in remaining
                     if s.get("parent_step_id") is None or s["parent_step_id"] in id_map]
            if not batch:
                break
            for snap in batch:
                new_parent = id_map.get(snap["parent_step_id"]) if snap.get("parent_step_id") else None
                step = FunnelStep(
                    funnel_id=funnel_id,
                    step_type=snap["step_type"],
                    step_config=snap.get("step_config", {}),
                    position=snap["position"],
                    parent_step_id=new_parent,
                    branch=snap.get("branch", "main"),
                )
                self.db.add(step)
                self.db.flush()
                id_map[snap["id"]] = step.id
                remaining.remove(snap)

        self.db.commit()
        return self._funnel_response(funnel)

    # ------------------------------------------------------------------
    # Response builder
    # ------------------------------------------------------------------

    def _funnel_response(self, funnel: Funnel) -> dict:
        sc, ec = self._counts(funnel.id)
        steps = (
            self.db.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel.id)
            .order_by(FunnelStep.position)
            .all()
        )
        return {
            **{c.name: getattr(funnel, c.name) for c in funnel.__table__.columns},
            "steps": steps,
            "step_count": sc,
            "enrollment_count": ec,
        }

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    def _extract_references(self, steps_data: list, project_id: int) -> list:
        """Scan step configs for project-specific ID references and look up labels."""
        refs: dict = {}  # key = (ref_type, original_id)

        for step in steps_data:
            cfg = step.get("step_config", {}) or {}
            export_id = step.get("_export_id")

            for ref_type, config_key, condition_fn in REFERENCE_PATHS:
                if condition_fn and not condition_fn(cfg):
                    continue

                # Extract the actual ID value
                if config_key == "on_topic_handler":
                    handler = cfg.get("on_topic_handler")
                    if isinstance(handler, dict) and handler.get("id"):
                        ref_id = handler["id"]
                    else:
                        continue
                    path = "step_config.on_topic_handler.id"
                else:
                    ref_id = cfg.get(config_key)
                    path = f"step_config.{config_key}"

                if not isinstance(ref_id, int):
                    continue

                key = (ref_type, ref_id)
                if key not in refs:
                    refs[key] = {
                        "ref_type": ref_type,
                        "original_id": ref_id,
                        "label": "",
                        "found_in": [],
                    }
                refs[key]["found_in"].append({"step_export_id": export_id, "path": path})

        # Look up labels
        for (ref_type, ref_id), ref in refs.items():
            label = str(ref_id)
            if ref_type == "whatsapp_instance":
                entity = self.db.query(WhatsAppInstance).filter(WhatsAppInstance.id == ref_id).first()
                if entity:
                    label = entity.instance_name or label
            elif ref_type == "chatbot":
                entity = self.db.query(Chatbot).filter(Chatbot.id == ref_id).first()
                if entity:
                    label = entity.name or label
            elif ref_type == "agent_team":
                entity = self.db.query(AgentTeam).filter(AgentTeam.id == ref_id).first()
                if entity:
                    label = entity.name or label
            elif ref_type == "email_template":
                entity = self.db.query(MessagingTemplate).filter(MessagingTemplate.id == ref_id).first()
                if entity:
                    label = entity.name or label
            elif ref_type == "api_connection":
                entity = self.db.query(ProjectApiConnection).filter(ProjectApiConnection.id == ref_id).first()
                if entity:
                    label = entity.name or label
            elif ref_type == "email_instance":
                entity = resolve_email_instance(
                    self.db, project_id=project_id, instance_id=ref_id, require_ready=False,
                )
                if entity:
                    label = entity.instance_name or label
            ref["label"] = label

        return list(refs.values())

    def export_funnel(self, funnel_id: int, workspace_id: int) -> dict:
        """Export a funnel as a self-documenting JSON structure."""
        funnel = self._get_funnel(funnel_id, workspace_id)
        steps = (
            self.db.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel_id)
            .order_by(FunnelStep.position)
            .all()
        )

        # Build id_to_export_id map
        id_to_export_id = {}
        for idx, step in enumerate(steps, 1):
            id_to_export_id[step.id] = idx

        # Build steps array
        steps_data = []
        for step in steps:
            steps_data.append({
                "_export_id": id_to_export_id[step.id],
                "step_type": step.step_type,
                "step_config": step.step_config or {},
                "position": step.position,
                "parent_export_id": id_to_export_id.get(step.parent_step_id) if step.parent_step_id else None,
                "branch": step.branch or "main",
            })

        # Extract references
        references = self._extract_references(steps_data, funnel.project_id)

        return {
            "versya_funnel_export": True,
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "funnel": {
                "name": funnel.name,
                "description": funnel.description,
                "trigger_type": funnel.trigger_type,
                "trigger_config": funnel.trigger_config or {},
                "global_exit_config": funnel.global_exit_config or {},
                "purpose_key": funnel.purpose_key,
                "attention_policy": funnel.attention_policy,
            },
            "steps": steps_data,
            "references": references,
        }

    def import_funnel(self, project_id: int, workspace_id: int, user_id: int, payload: dict) -> dict:
        """Import a funnel from an export JSON payload."""
        if not payload.get("versya_funnel_export"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid export file")
        if payload.get("version") != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported export version")

        self._get_project(project_id, workspace_id)
        funnel_data = payload.get("funnel", {})

        # Create funnel in draft
        funnel = Funnel(
            project_id=project_id,
            name=funnel_data.get("name", "Imported Funnel") + " (Imported)",
            description=funnel_data.get("description"),
            trigger_type=funnel_data.get("trigger_type", "event"),
            trigger_config=funnel_data.get("trigger_config", {}),
            global_exit_config=funnel_data.get("global_exit_config", {}),
            purpose_key=funnel_data.get("purpose_key"),
            attention_policy=funnel_data.get("attention_policy"),
            created_by=user_id,
            status="draft",
        )
        self.db.add(funnel)
        self.db.flush()

        # Recreate steps with topological sort (parent_export_id deps)
        export_id_to_real_id: dict = {}
        remaining = list(payload.get("steps", []))
        max_iter = len(remaining) + 1
        while remaining and max_iter > 0:
            max_iter -= 1
            batch = [s for s in remaining
                     if s.get("parent_export_id") is None or s["parent_export_id"] in export_id_to_real_id]
            if not batch:
                break
            for snap in batch:
                parent_real_id = export_id_to_real_id.get(snap["parent_export_id"]) if snap.get("parent_export_id") else None
                step = FunnelStep(
                    funnel_id=funnel.id,
                    step_type=snap["step_type"],
                    step_config=snap.get("step_config", {}),
                    position=snap.get("position", 0),
                    parent_step_id=parent_real_id,
                    branch=snap.get("branch", "main"),
                )
                self.db.add(step)
                self.db.flush()
                export_id_to_real_id[snap["_export_id"]] = step.id
                remaining.remove(snap)

        self.db.commit()
        self.db.refresh(funnel)

        # Check unresolved references
        unresolved = self.check_references(funnel.id, workspace_id)

        step_count = self.db.query(func.count(FunnelStep.id)).filter(FunnelStep.funnel_id == funnel.id).scalar() or 0

        return {
            "funnel_id": funnel.id,
            "name": funnel.name,
            "status": funnel.status,
            "step_count": step_count,
            "unresolved_references": [r for r in unresolved if not r["resolved"]],
        }

    def check_references(self, funnel_id: int, workspace_id: int) -> list:
        """Check which project-specific references in step configs are resolved."""
        funnel = self._get_funnel(funnel_id, workspace_id)
        steps = (
            self.db.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel_id)
            .order_by(FunnelStep.position)
            .all()
        )

        results = []
        seen = set()

        for step in steps:
            cfg = step.step_config or {}

            for ref_type, config_key, condition_fn in REFERENCE_PATHS:
                if condition_fn and not condition_fn(cfg):
                    continue

                if config_key == "on_topic_handler":
                    handler = cfg.get("on_topic_handler")
                    if isinstance(handler, dict) and handler.get("id"):
                        ref_id = handler["id"]
                    else:
                        continue
                    path = "step_config.on_topic_handler.id"
                else:
                    ref_id = cfg.get(config_key)
                    path = f"step_config.{config_key}"

                if not isinstance(ref_id, int):
                    continue

                key = (ref_type, ref_id)
                if key in seen:
                    # Add to found_in of existing
                    for r in results:
                        if r["ref_type"] == ref_type and r["current_id"] == ref_id:
                            r["found_in"].append({"step_id": step.id, "path": path})
                    continue
                seen.add(key)

                # Check if entity exists in the project
                resolved = False
                if ref_type == "whatsapp_instance":
                    project = self.db.query(Project).filter(Project.id == funnel.project_id).first()
                    resolved = self.db.query(WhatsAppInstance).filter(
                        WhatsAppInstance.id == ref_id,
                        WhatsAppInstance.workspace_id == project.workspace_id if project else -1,
                    ).first() is not None
                elif ref_type == "chatbot":
                    resolved = self.db.query(Chatbot).filter(
                        Chatbot.id == ref_id, Chatbot.project_id == funnel.project_id
                    ).first() is not None
                elif ref_type == "agent_team":
                    resolved = self.db.query(AgentTeam).filter(
                        AgentTeam.id == ref_id, AgentTeam.project_id == funnel.project_id
                    ).first() is not None
                elif ref_type == "email_template":
                    resolved = self.db.query(MessagingTemplate).filter(
                        MessagingTemplate.id == ref_id, MessagingTemplate.project_id == funnel.project_id
                    ).first() is not None
                elif ref_type == "api_connection":
                    resolved = self.db.query(ProjectApiConnection).filter(
                        ProjectApiConnection.id == ref_id, ProjectApiConnection.project_id == funnel.project_id
                    ).first() is not None
                elif ref_type == "email_instance":
                    resolved = resolve_email_instance(
                        self.db, project_id=funnel.project_id, instance_id=ref_id, require_ready=True,
                    ) is not None

                # Get options (all entities of that type in the project)
                options = []
                if ref_type == "whatsapp_instance":
                    project = self.db.query(Project).filter(Project.id == funnel.project_id).first()
                    if project:
                        entities = self.db.query(WhatsAppInstance).filter(
                            WhatsAppInstance.workspace_id == project.workspace_id,
                            WhatsAppInstance.is_active == True,
                        ).all()
                        options = [{"id": e.id, "name": e.instance_name or str(e.id)} for e in entities]
                elif ref_type == "chatbot":
                    entities = self.db.query(Chatbot).filter(Chatbot.project_id == funnel.project_id).all()
                    options = [{"id": e.id, "name": e.name or str(e.id)} for e in entities]
                elif ref_type == "agent_team":
                    entities = self.db.query(AgentTeam).filter(AgentTeam.project_id == funnel.project_id).all()
                    options = [{"id": e.id, "name": e.name or str(e.id)} for e in entities]
                elif ref_type == "email_template":
                    entities = self.db.query(MessagingTemplate).filter(MessagingTemplate.project_id == funnel.project_id).all()
                    options = [{"id": e.id, "name": e.name or str(e.id)} for e in entities]
                elif ref_type == "api_connection":
                    entities = self.db.query(ProjectApiConnection).filter(ProjectApiConnection.project_id == funnel.project_id).all()
                    options = [{"id": e.id, "name": e.name or str(e.id)} for e in entities]
                elif ref_type == "email_instance":
                    project = self.db.query(Project).filter(Project.id == funnel.project_id).first()
                    if project:
                        from app.models import EmailInstance
                        entities = self.db.query(EmailInstance).filter(
                            EmailInstance.workspace_id == project.workspace_id,
                            ((EmailInstance.project_id == funnel.project_id) | (EmailInstance.project_id.is_(None))),
                            EmailInstance.is_active == True,  # noqa: E712
                            EmailInstance.connection_status == "verified",
                        ).all()
                        options = [{"id": e.id, "name": e.instance_name or str(e.id)} for e in entities]

                results.append({
                    "ref_type": ref_type,
                    "current_id": ref_id,
                    "resolved": resolved,
                    "found_in": [{"step_id": step.id, "path": path}],
                    "options": options,
                })

        return results

    def resolve_references(self, funnel_id: int, workspace_id: int, resolutions: list) -> dict:
        """Bulk-replace reference IDs in step configs."""
        funnel = self._get_funnel(funnel_id, workspace_id)
        steps = (
            self.db.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel_id)
            .all()
        )

        resolved_count = 0
        for resolution in resolutions:
            ref_type = resolution["ref_type"]
            original_id = resolution["original_id"]
            resolved_id = resolution["resolved_id"]

            for step in steps:
                cfg = step.step_config or {}
                modified = False

                for rt, config_key, condition_fn in REFERENCE_PATHS:
                    if rt != ref_type:
                        continue
                    if condition_fn and not condition_fn(cfg):
                        continue

                    if config_key == "on_topic_handler":
                        handler = cfg.get("on_topic_handler")
                        if isinstance(handler, dict) and handler.get("id") == original_id:
                            handler["id"] = resolved_id
                            modified = True
                    else:
                        if cfg.get(config_key) == original_id:
                            cfg[config_key] = resolved_id
                            modified = True

                if modified:
                    step.step_config = cfg
                    flag_modified(step, "step_config")
                    resolved_count += 1

        self.db.commit()

        # Re-check
        remaining = self.check_references(funnel_id, workspace_id)
        unresolved_count = sum(1 for r in remaining if not r["resolved"])

        return {
            "resolved_count": resolved_count,
            "unresolved_count": unresolved_count,
        }
