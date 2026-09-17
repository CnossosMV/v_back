"""Optimistic, project-scoped orchestration ownership changes."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.project_import import LifecycleModel, ProjectLifecycleCutover


class OrchestrationCutoverError(ValueError):
    pass


class OrchestrationCutoverService:
    def __init__(self, db: Session):
        self.db = db

    def get(self, project_id: int, purpose_key: str, *, for_update: bool = False) -> ProjectLifecycleCutover | None:
        query = self.db.query(ProjectLifecycleCutover).filter(
            ProjectLifecycleCutover.project_id == project_id,
            ProjectLifecycleCutover.purpose_key == purpose_key,
        )
        return query.with_for_update().first() if for_update else query.first()

    def execution_gate(self, project_id: int, purpose_key: str | None) -> dict:
        """Return the side-effect gate for a purpose-aware Versya automation.

        A null purpose is backward compatible. Once an automation declares a
        purpose, absence of a cutover row means the legacy system still owns
        it, so Versya must remain inert.
        """
        if not purpose_key:
            return {"allowed": True, "mode": "unmanaged", "orchestration_epoch": None}
        row = self.get(project_id, purpose_key)
        mode = row.mode if row else "legacy"
        epoch = int(row.orchestration_epoch or 0) if row else 0
        return {
            "allowed": mode == "versya",
            "mode": mode,
            "purpose_key": purpose_key,
            "orchestration_epoch": epoch,
        }

    def legacy_decision_block_reason(self, project_id: int, properties: dict | None) -> dict | None:
        """Reject stale/final legacy decisions after an ownership change.

        Facts and historical events are never rejected here. Older producers
        without the semantic metadata remain backward compatible.
        """
        properties = properties or {}
        semantic_kind = (
            properties.get("orchestration_semantic_kind")
            or properties.get("event_semantic_kind")
            or properties.get("semantic_kind")
        )
        purpose_key = properties.get("purpose_key")
        if semantic_kind != "decision" or not purpose_key:
            return None
        raw_epoch = properties.get("orchestration_epoch", 0)
        try:
            event_epoch = int(raw_epoch)
        except (TypeError, ValueError):
            return {"code": "invalid_orchestration_epoch", "purpose_key": purpose_key}
        row = self.get(project_id, str(purpose_key))
        if row is None:
            if event_epoch > 0:
                return {
                    "code": "cutover_not_mirrored",
                    "purpose_key": purpose_key,
                    "event_epoch": event_epoch,
                    "current_epoch": 0,
                }
            return None
        current_epoch = int(row.orchestration_epoch or 0)
        if event_epoch != current_epoch:
            return {
                "code": "stale_or_future_orchestration_epoch",
                "purpose_key": purpose_key,
                "event_epoch": event_epoch,
                "current_epoch": current_epoch,
                "mode": row.mode,
            }
        if row.mode == "versya":
            return {
                "code": "legacy_decision_after_versya_cutover",
                "purpose_key": purpose_key,
                "event_epoch": event_epoch,
                "current_epoch": current_epoch,
                "mode": row.mode,
            }
        return None

    def preview(self, project_id: int, purpose_key: str, mode: str, expected_epoch: int, lifecycle_model_id: int | None) -> dict:
        row = self.get(project_id, purpose_key)
        current_mode = row.mode if row else "legacy"
        current_epoch = row.orchestration_epoch if row else 0
        blockers = []
        if current_epoch != expected_epoch:
            blockers.append({"code": "stale_epoch", "current_epoch": current_epoch})
        model = None
        if lifecycle_model_id is not None:
            model = self.db.query(LifecycleModel).filter(
                LifecycleModel.id == lifecycle_model_id,
                LifecycleModel.project_id == project_id,
            ).first()
            if not model:
                blockers.append({"code": "model_scope", "message": "Lifecycle model does not belong to project"})
        if mode == "versya" and (not model or model.status != "active"):
            blockers.append({"code": "active_model_required", "message": "Versya mode requires an active lifecycle model"})
        return {
            "purpose_key": purpose_key,
            "from_mode": current_mode,
            "to_mode": mode,
            "current_epoch": current_epoch,
            "next_epoch": current_epoch + 1,
            "lifecycle_model_id": lifecycle_model_id,
            "valid": not blockers,
            "blockers": blockers,
            "rollback": {"mode": current_mode, "expected_epoch": current_epoch + 1},
        }

    def update(
        self,
        project_id: int,
        *,
        purpose_key: str,
        mode: str,
        expected_epoch: int,
        lifecycle_model_id: int | None,
        reason: str,
        actor_user_id: int | None,
    ) -> ProjectLifecycleCutover:
        row = self.get(project_id, purpose_key, for_update=True)
        if row is None:
            if expected_epoch != 0:
                raise OrchestrationCutoverError("Cutover does not exist; expected_epoch must be 0")
            row = ProjectLifecycleCutover(
                project_id=project_id,
                purpose_key=purpose_key,
                mode="legacy",
                orchestration_epoch=0,
            )
            self.db.add(row)
            self.db.flush()
        preview = self.preview(project_id, purpose_key, mode, expected_epoch, lifecycle_model_id)
        if not preview["valid"]:
            raise OrchestrationCutoverError(str(preview["blockers"]))
        row.previous_mode = row.mode
        row.mode = mode
        row.lifecycle_model_id = lifecycle_model_id
        row.orchestration_epoch += 1
        row.reason = reason
        row.changed_by_user_id = actor_user_id
        row.changed_at = datetime.utcnow()
        self.db.flush()
        return row

    def rollback(
        self,
        project_id: int,
        *,
        purpose_key: str,
        expected_epoch: int,
        reason: str,
        actor_user_id: int | None,
    ) -> ProjectLifecycleCutover:
        current = self.get(project_id, purpose_key)
        if not current:
            raise OrchestrationCutoverError("Cutover does not exist")
        target = current.previous_mode or "legacy"
        model_id = current.lifecycle_model_id if target in {"shadow", "versya"} else None
        return self.update(
            project_id,
            purpose_key=purpose_key,
            mode=target,
            expected_epoch=expected_epoch,
            lifecycle_model_id=model_id,
            reason=reason,
            actor_user_id=actor_user_id,
        )
