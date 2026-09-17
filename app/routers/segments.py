"""
Segment Rules Router

CRUD for segment rules, reorder, preview, test, and batch evaluation.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime
from typing import List

from app.database import get_db
from app.models import User, Project, SegmentRule
from app.models.messaging import MessagingUser
from app.schemas.segments import (
    SegmentRuleCreate, SegmentRuleUpdate, SegmentRuleResponse,
    SegmentRuleReorder, SegmentPreviewResponse, SegmentPreviewItem,
    SegmentTestResponse, SegmentTestRuleResult,
    SegmentBatchEvalResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(tags=["Segments"])


# ============================================================================
# Helpers
# ============================================================================

def _get_project(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _get_rule(db: Session, rule_id: int) -> SegmentRule:
    rule = db.query(SegmentRule).filter(SegmentRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Segment rule not found")
    return rule


def _rule_to_response(db: Session, rule: SegmentRule) -> SegmentRuleResponse:
    contact_count = db.query(func.count(MessagingUser.id)).filter(
        MessagingUser.segment_rule_id == rule.id,
    ).scalar() or 0

    return SegmentRuleResponse(
        id=rule.id,
        project_id=rule.project_id,
        name=rule.name,
        description=rule.description,
        priority=rule.priority,
        conditions=rule.conditions or [],
        match_mode=rule.match_mode,
        is_catch_all=rule.is_catch_all,
        is_active=rule.is_active,
        contact_count=contact_count,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


# ============================================================================
# CRUD
# ============================================================================

@router.get("/projects/{project_id}/segments")
def list_rules(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    rules = db.query(SegmentRule).filter(
        SegmentRule.project_id == project_id,
    ).order_by(SegmentRule.priority).all()
    return [_rule_to_response(db, r) for r in rules]


@router.post("/projects/{project_id}/segments")
def create_rule(
    project_id: int,
    data: SegmentRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    # Check name uniqueness within project
    existing = db.query(SegmentRule).filter(
        SegmentRule.project_id == project_id,
        SegmentRule.name == data.name,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Segment name '{data.name}' already exists")

    # If catch-all, validate it will be last
    if data.is_catch_all:
        non_catch_all_after = db.query(SegmentRule).filter(
            SegmentRule.project_id == project_id,
            SegmentRule.is_catch_all == True,
        ).first()
        if non_catch_all_after:
            raise HTTPException(status_code=400, detail="Only one catch-all rule is allowed")

    # Auto-assign priority = max + 1
    max_priority = db.query(func.max(SegmentRule.priority)).filter(
        SegmentRule.project_id == project_id,
    ).scalar()
    priority = (max_priority or 0) + 1

    rule = SegmentRule(
        project_id=project_id,
        name=data.name,
        description=data.description,
        priority=priority,
        conditions=[c.model_dump() for c in data.conditions],
        match_mode=data.match_mode,
        is_catch_all=data.is_catch_all,
        is_active=data.is_active,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_to_response(db, rule)


@router.get("/segments/{rule_id}")
def get_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule = _get_rule(db, rule_id)
    _get_project(db, rule.project_id, current_user)
    return _rule_to_response(db, rule)


@router.put("/segments/{rule_id}")
def update_rule(
    rule_id: int,
    data: SegmentRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule = _get_rule(db, rule_id)
    _get_project(db, rule.project_id, current_user)

    if data.name is not None:
        # Check name uniqueness
        existing = db.query(SegmentRule).filter(
            SegmentRule.project_id == rule.project_id,
            SegmentRule.name == data.name,
            SegmentRule.id != rule.id,
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Segment name '{data.name}' already exists")
        rule.name = data.name

    if data.description is not None:
        rule.description = data.description
    if data.conditions is not None:
        rule.conditions = [c.model_dump() for c in data.conditions]
    if data.match_mode is not None:
        rule.match_mode = data.match_mode
    if data.is_catch_all is not None:
        if data.is_catch_all:
            existing_catch = db.query(SegmentRule).filter(
                SegmentRule.project_id == rule.project_id,
                SegmentRule.is_catch_all == True,
                SegmentRule.id != rule.id,
            ).first()
            if existing_catch:
                raise HTTPException(status_code=400, detail="Only one catch-all rule is allowed")
        rule.is_catch_all = data.is_catch_all
    if data.is_active is not None:
        rule.is_active = data.is_active

    rule.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(rule)
    return _rule_to_response(db, rule)


@router.delete("/segments/{rule_id}")
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rule = _get_rule(db, rule_id)
    project = _get_project(db, rule.project_id, current_user)

    # Clear segment on affected contacts and emit segment_changed events
    affected_users = db.query(MessagingUser).filter(
        MessagingUser.segment_rule_id == rule.id,
    ).all()

    from app.services.segment_engine import SegmentEngine
    engine = SegmentEngine(db)

    for user in affected_users:
        prev = {"id": rule.id, "name": rule.name}
        user.segment_rule_id = None
        user.segment_name = None
        user.segment_updated_at = datetime.utcnow()
        engine._emit_segment_changed(project.id, user.id, prev, None, "rule_deleted")

    db.delete(rule)
    db.flush()

    # Re-compact priorities
    remaining = db.query(SegmentRule).filter(
        SegmentRule.project_id == project.id,
    ).order_by(SegmentRule.priority).all()

    for i, r in enumerate(remaining):
        r.priority = i + 1

    db.commit()
    return {"detail": "Deleted", "affected_contacts": len(affected_users)}


# ============================================================================
# Reorder
# ============================================================================

@router.post("/projects/{project_id}/segments/reorder")
def reorder_rules(
    project_id: int,
    data: SegmentRuleReorder,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    rules = db.query(SegmentRule).filter(
        SegmentRule.project_id == project_id,
    ).all()

    rule_map = {r.id: r for r in rules}

    # Validate all IDs belong to this project
    for rid in data.rule_ids:
        if rid not in rule_map:
            raise HTTPException(status_code=400, detail=f"Rule ID {rid} not found in project")

    # Update priorities based on new order
    for i, rid in enumerate(data.rule_ids):
        rule_map[rid].priority = i + 1

    db.commit()

    updated = db.query(SegmentRule).filter(
        SegmentRule.project_id == project_id,
    ).order_by(SegmentRule.priority).all()
    return [_rule_to_response(db, r) for r in updated]


# ============================================================================
# Preview / Test / Evaluate
# ============================================================================

@router.post("/projects/{project_id}/segments/preview")
def preview_rules(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    from app.services.segment_engine import SegmentEngine
    engine = SegmentEngine(db)
    items, unmatched = engine.preview_rules(project_id)

    return SegmentPreviewResponse(
        items=[SegmentPreviewItem(**item) for item in items],
        unmatched_count=unmatched,
    )


@router.post("/projects/{project_id}/segments/test/{user_id}")
def test_user(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    from app.services.segment_engine import SegmentEngine
    engine = SegmentEngine(db)
    result = engine.test_user(project_id, user_id)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return SegmentTestResponse(
        user_id=result["user_id"],
        current_segment=result["current_segment"],
        evaluated_segment=result["evaluated_segment"],
        results=[SegmentTestRuleResult(**r) for r in result["results"]],
    )


@router.post("/projects/{project_id}/segments/evaluate")
def trigger_evaluation(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    from app.services.segment_engine import SegmentEngine
    engine = SegmentEngine(db)
    result = engine.batch_evaluate_project(project_id)

    return SegmentBatchEvalResponse(**result)
