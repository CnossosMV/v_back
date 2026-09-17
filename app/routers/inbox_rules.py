"""
Inbox Assignment Rules — CRUD + reorder endpoints.

Rules determine how new inbound messages (without an existing routing state) are routed.
Evaluated in priority order; first match wins.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import InboxAssignmentRule, Project
from app.schemas.inbound_router import (
    InboxRuleCreate, InboxRuleUpdate, InboxRuleResponse, InboxRuleReorderRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Inbox Rules"])


def _get_project(db: Session, project_id: int, user_info: dict) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/inbox-rules", response_model=List[InboxRuleResponse])
def list_rules(
    project_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    _get_project(db, project_id, user_info)
    rules = (
        db.query(InboxAssignmentRule)
        .filter(InboxAssignmentRule.project_id == project_id)
        .order_by(InboxAssignmentRule.priority.desc())
        .all()
    )
    return rules


@router.post("/projects/{project_id}/inbox-rules", response_model=InboxRuleResponse)
def create_rule(
    project_id: int,
    payload: InboxRuleCreate,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    _get_project(db, project_id, user_info)
    rule = InboxAssignmentRule(
        project_id=project_id,
        name=payload.name,
        priority=payload.priority,
        conditions=payload.conditions,
        match_mode=payload.match_mode,
        destination_type=payload.destination_type,
        destination_id=payload.destination_id,
        is_active=payload.is_active,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/inbox-rules/{rule_id}", response_model=InboxRuleResponse)
def update_rule(
    rule_id: int,
    payload: InboxRuleUpdate,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    rule = db.query(InboxAssignmentRule).filter(InboxAssignmentRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)

    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/inbox-rules/{rule_id}")
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    rule = db.query(InboxAssignmentRule).filter(InboxAssignmentRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    return {"success": True}


@router.post("/projects/{project_id}/inbox-rules/reorder")
def reorder_rules(
    project_id: int,
    payload: InboxRuleReorderRequest,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    _get_project(db, project_id, user_info)
    # Assign decreasing priorities so first in list = highest priority
    total = len(payload.rule_ids)
    for idx, rule_id in enumerate(payload.rule_ids):
        rule = db.query(InboxAssignmentRule).filter(
            InboxAssignmentRule.id == rule_id,
            InboxAssignmentRule.project_id == project_id,
        ).first()
        if rule:
            rule.priority = total - idx
    db.commit()
    return {"success": True}
