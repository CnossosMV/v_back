"""Project-scoped lifecycle engine rollout and operational metrics."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import ArmObservation, ContactPosition, SendLog
from app.models.engine_control import ProjectEngineRolloutEvent
from app.schemas.engine import EngineRolloutRequest
from app.services.engine_rollout_service import EngineRolloutService

router = APIRouter(prefix="/projects/{project_id}/engine", tags=["engine"])


def _counts(db: Session, project_id: int) -> dict:
    rows = db.query(SendLog.status, func.count().label("c")).filter(SendLog.project_id == project_id).group_by(SendLog.status).all()
    ledger = db.execute(text("""
        SELECT
          count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM contact_ledger cl
            WHERE cl.project_id = sl.project_id
              AND cl.user_id = sl.user_id
              AND cl.channel = sl.channel
              AND cl.source = sl.source_type
              AND cl.source_id = CAST(sl.id AS text)
          )) AS matched,
          count(*) AS expected
        FROM send_logs sl
        WHERE sl.project_id = :pid
          AND sl.status IN ('sent', 'delivered', 'read', 'opened', 'submission_unknown')
          AND sl.user_id IS NOT NULL
          AND COALESCE(sl.sent_at, sl.queued_at) >= now() - interval '7 days'
    """), {"pid": project_id}).mappings().one()
    duplicates = db.execute(text("""
        SELECT count(*) FROM (
          SELECT source, source_id, user_id, channel
          FROM contact_ledger
          WHERE project_id = :pid AND source_id IS NOT NULL
            AND sent_at >= now() - interval '7 days'
          GROUP BY source, source_id, user_id, channel
          HAVING count(*) > 1
        ) duplicate_keys
    """), {"pid": project_id}).scalar() or 0
    expected = int(ledger["expected"] or 0)
    matched = int(ledger["matched"] or 0)
    return {
        "arm_observations": db.query(func.count(ArmObservation.id)).filter(ArmObservation.project_id == project_id).scalar() or 0,
        "contact_positions": db.query(func.count(ContactPosition.id)).filter(ContactPosition.project_id == project_id).scalar() or 0,
        "send_logs_by_status": {row.status: row.c for row in rows},
        "ledger_parity": {
            "window_days": 7,
            "expected": expected,
            "matched": matched,
            "ratio": (matched / expected) if expected else None,
            "duplicate_keys": int(duplicates),
        },
    }


def _history(db: Session, project_id: int) -> list[dict]:
    rows = db.query(ProjectEngineRolloutEvent).filter(
        ProjectEngineRolloutEvent.project_id == project_id,
    ).order_by(ProjectEngineRolloutEvent.created_at.desc()).limit(100).all()
    return [{
        "id": row.id, "feature_key": row.feature_key,
        "previous_mode": row.previous_mode, "new_mode": row.new_mode,
        "actor_user_id": row.actor_user_id, "created_at": row.created_at,
    } for row in rows]


@router.get("/status")
def get_engine_status(project_id: int, db: Session = Depends(get_db), _auth=Depends(require_project_role("viewer"))):
    return {
        "features": EngineRolloutService(db).list_features(project_id),
        "counts": _counts(db, project_id),
        "history": _history(db, project_id),
    }


@router.get("/rollout")
def get_rollout(project_id: int, db: Session = Depends(get_db), _auth=Depends(require_project_role("viewer"))):
    return {
        "features": EngineRolloutService(db).list_features(project_id),
        "history": _history(db, project_id),
    }


@router.put("/rollout")
def put_rollout(payload: EngineRolloutRequest, project_id: int, db: Session = Depends(get_db), auth=Depends(require_project_role("admin"))):
    try:
        features = EngineRolloutService(db).update(project_id, [item.model_dump(exclude_unset=True) for item in payload.updates], auth["user_id"])
        return {"features": features}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail={"code": "missing_dependencies", "dependencies": exc.args[0]}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rollout/validate")
def validate_rollout(payload: EngineRolloutRequest, project_id: int, db: Session = Depends(get_db), _auth=Depends(require_project_role("admin"))):
    try:
        errors = EngineRolloutService(db).validate(project_id, [item.model_dump(exclude_unset=True) for item in payload.updates])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    consequence = None
    future_update = next(
        (item for item in payload.updates if item.feature_key == "future_plan"),
        None,
    )
    if future_update:
        from app.services.channels.future_attention_planner import FutureAttentionPlanner
        from app.services.channels.selection import future_plan_config

        config = future_plan_config(
            db,
            project_id,
            override_config=future_update.config or {},
        )
        effective_mode = EngineRolloutService(db).effective_modes(
            project_id, {"future_plan": future_update.mode},
        )["future_plan"]
        consequence = FutureAttentionPlanner(db).configuration_consequence(
            project_id,
            mode=future_update.mode,
            config=config,
            effective_mode=effective_mode,
        )
    return {
        "valid": not errors,
        "dependencies": errors,
        "consequence_at_gate": consequence,
        "external_sends": 0,
    }


@router.get("/attention-plan/contacts/{contact_id}")
def get_contact_attention_plan(
    contact_id: int,
    project_id: int,
    as_of: datetime | None = None,
    horizon_minutes: int | None = None,
    collision_window_minutes: int | None = None,
    db: Session = Depends(get_db),
    _auth=Depends(require_project_role("viewer")),
):
    from app.models.messaging import MessagingUser
    from app.services.channels.future_attention_planner import FutureAttentionPlanner

    contact = db.query(MessagingUser.id).filter(
        MessagingUser.id == contact_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    try:
        return FutureAttentionPlanner(db).preview_contact(
            project_id,
            user_id=contact_id,
            as_of=as_of,
            horizon_minutes=horizon_minutes,
            collision_window_minutes=collision_window_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/metrics")
def get_engine_metrics(project_id: int, db: Session = Depends(get_db), _auth=Depends(require_project_role("viewer"))):
    return {"counts": _counts(db, project_id)}


@router.get("/locale-breakdown")
def get_locale_breakdown(project_id: int, db: Session = Depends(get_db), _auth=Depends(require_project_role("viewer"))):
    from app.models.messaging import MessagingUser
    sends = db.query(MessagingUser.locale, func.count(SendLog.id).label("c")).outerjoin(MessagingUser, SendLog.user_id == MessagingUser.id).filter(SendLog.project_id == project_id).group_by(MessagingUser.locale).all()
    contacts = db.query(MessagingUser.locale, func.count(MessagingUser.id).label("c")).filter(MessagingUser.project_id == project_id).group_by(MessagingUser.locale).all()
    return {"sends_by_locale": {(r.locale or "unset"): r.c for r in sends}, "contacts_by_locale": {(r.locale or "unset"): r.c for r in contacts}}
