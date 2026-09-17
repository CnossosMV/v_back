"""
Bandit recommend API (Phase 4) — surfaces the execution bandit's per-slot
recommendation (incumbent vs challenger, earned_auto, effect size, confidence)
for the recommend UI. Read-only; recommend-by-default, never auto-applies here.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import Project
from app.services.bandit.reward import RewardService, VALID_CLASSES

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/bandit",
    tags=["bandit"],
)


@router.get("/calibration")
def get_calibration(
    project_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Gate-threshold calibration: sweep candidate thresholds over reconstructed
    send/reply history and report graduation counts + a recommendation. Run this
    BEFORE enabling earned_auto."""
    if not db.query(Project).filter(Project.id == project_id).first():
        raise HTTPException(status_code=404, detail="Project not found")
    from app.services.bandit.simulator import BanditSimulator
    return BanditSimulator(db).calibrate(project_id)


@router.get("/slots")
def list_bandit_slots(
    project_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Slots that have bandit evidence — (scope_key, decision_class, arms, trials)
    so the recommend UI can browse them."""
    if not db.query(Project).filter(Project.id == project_id).first():
        raise HTTPException(status_code=404, detail="Project not found")
    from sqlalchemy import func
    from app.models import ArmObservation
    rows = db.query(
        ArmObservation.scope_key,
        ArmObservation.decision_class,
        func.count(func.distinct(ArmObservation.arm_key)).label("arms"),
        func.count().label("trials"),
    ).filter(
        ArmObservation.project_id == project_id,
    ).group_by(
        ArmObservation.scope_key, ArmObservation.decision_class,
    ).order_by(func.count().desc()).limit(200).all()
    return {
        "slots": [
            {
                "scope_key": r.scope_key,
                "decision_class": r.decision_class,
                "slot_id": r.scope_key.split(":", 1)[1] if ":" in r.scope_key else r.scope_key,
                "arms": r.arms,
                "trials": r.trials,
            }
            for r in rows
        ],
    }


@router.get("/slots/{slot_id}/recommendation")
def get_slot_recommendation(
    project_id: int,
    slot_id: str,
    decision_class: str = Query("channel"),
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Per-slot bandit recommendation for one execution decision class.
    Arms default to observed-by-trials; the gate decides whether a challenger
    has earned auto or the authored ordering stands."""
    if not db.query(Project).filter(Project.id == project_id).first():
        raise HTTPException(status_code=404, detail="Project not found")
    if decision_class not in VALID_CLASSES:
        raise HTTPException(status_code=400, detail=f"decision_class must be one of {VALID_CLASSES}")
    return RewardService(db).recommendation(project_id, decision_class, slot_id)
