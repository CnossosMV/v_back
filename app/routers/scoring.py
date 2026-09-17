"""
Scoring Router

CRUD for score definitions, user scores, analytics, and batch operations.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime
from typing import Optional, List

from app.database import get_db
from app.models import (
    User, Project, ScoreDefinition, UserScoreSnapshot, UserFeatureStore,
)
from app.schemas.scoring import (
    ScoreDefinitionCreate, ScoreDefinitionUpdate, ScoreDefinitionResponse,
    UserScoreResponse, ScoreExplanationResponse, ScoreExplanationItem,
    ScoreDistributionBucket, TierCountResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(tags=["Scoring"])


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


def _get_definition(db: Session, definition_id: int) -> ScoreDefinition:
    defn = db.query(ScoreDefinition).filter(
        ScoreDefinition.id == definition_id,
    ).first()
    if not defn:
        raise HTTPException(status_code=404, detail="Score definition not found")
    return defn


def _defn_to_response(db: Session, defn: ScoreDefinition) -> ScoreDefinitionResponse:
    user_count = db.query(func.count(UserScoreSnapshot.id)).filter(
        UserScoreSnapshot.score_definition_id == defn.id,
    ).scalar() or 0

    return ScoreDefinitionResponse(
        id=defn.id,
        project_id=defn.project_id,
        name=defn.name,
        slug=defn.slug,
        description=defn.description,
        score_type=defn.score_type,
        version=defn.version,
        status=defn.status,
        signals=defn.signals or [],
        decay_config=defn.decay_config,
        thresholds=defn.thresholds,
        normalization_max=defn.normalization_max,
        recalc_on_event=defn.recalc_on_event,
        recalc_interval_minutes=defn.recalc_interval_minutes,
        last_recalc_at=defn.last_recalc_at,
        created_by=defn.created_by,
        created_at=defn.created_at,
        updated_at=defn.updated_at,
        user_count=user_count,
    )


# ============================================================================
# Score Definition CRUD
# ============================================================================

@router.get("/projects/{project_id}/scores")
def list_definitions(
    project_id: int,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)
    query = db.query(ScoreDefinition).filter(
        ScoreDefinition.project_id == project_id,
    )
    if status:
        query = query.filter(ScoreDefinition.status == status)

    definitions = query.order_by(ScoreDefinition.created_at.desc()).all()
    return [_defn_to_response(db, d) for d in definitions]


@router.post("/projects/{project_id}/scores")
def create_definition(
    project_id: int,
    data: ScoreDefinitionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    # Check slug uniqueness
    existing = db.query(ScoreDefinition).filter(
        ScoreDefinition.project_id == project_id,
        ScoreDefinition.slug == data.slug,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Slug '{data.slug}' already exists")

    defn = ScoreDefinition(
        project_id=project_id,
        name=data.name,
        slug=data.slug,
        description=data.description,
        score_type=data.score_type,
        signals=[s.model_dump() for s in data.signals],
        decay_config=data.decay_config.model_dump() if data.decay_config else None,
        thresholds=data.thresholds.model_dump() if data.thresholds else None,
        normalization_max=data.normalization_max,
        recalc_on_event=data.recalc_on_event,
        recalc_interval_minutes=data.recalc_interval_minutes,
        created_by=current_user.id,
    )
    db.add(defn)
    db.commit()
    db.refresh(defn)
    return _defn_to_response(db, defn)


@router.get("/scores/{definition_id}")
def get_definition(
    definition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)
    return _defn_to_response(db, defn)


@router.put("/scores/{definition_id}")
def update_definition(
    definition_id: int,
    data: ScoreDefinitionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)

    if data.name is not None:
        defn.name = data.name
    if data.description is not None:
        defn.description = data.description
    if data.signals is not None:
        defn.signals = [s.model_dump() for s in data.signals]
    if data.decay_config is not None:
        defn.decay_config = data.decay_config.model_dump()
    if data.thresholds is not None:
        defn.thresholds = data.thresholds.model_dump()
    if data.normalization_max is not None:
        defn.normalization_max = data.normalization_max
    if data.recalc_on_event is not None:
        defn.recalc_on_event = data.recalc_on_event
    if data.recalc_interval_minutes is not None:
        defn.recalc_interval_minutes = data.recalc_interval_minutes

    defn.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(defn)
    return _defn_to_response(db, defn)


@router.delete("/scores/{definition_id}")
def delete_definition(
    definition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)
    db.delete(defn)
    db.commit()
    return {"detail": "Deleted"}


# ============================================================================
# Status transitions
# ============================================================================

@router.post("/scores/{definition_id}/activate")
def activate_definition(
    definition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)
    defn.status = "active"
    defn.updated_at = datetime.utcnow()
    db.commit()
    return _defn_to_response(db, defn)


@router.post("/scores/{definition_id}/archive")
def archive_definition(
    definition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)
    defn.status = "archived"
    defn.updated_at = datetime.utcnow()
    db.commit()
    return _defn_to_response(db, defn)


@router.post("/scores/{definition_id}/duplicate")
def duplicate_definition(
    definition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)

    new_version = defn.version + 1
    new_slug = f"{defn.slug}_v{new_version}"

    clone = ScoreDefinition(
        project_id=defn.project_id,
        name=f"{defn.name} (v{new_version})",
        slug=new_slug,
        description=defn.description,
        score_type=defn.score_type,
        version=new_version,
        status="draft",
        signals=defn.signals,
        decay_config=defn.decay_config,
        thresholds=defn.thresholds,
        normalization_max=defn.normalization_max,
        recalc_on_event=defn.recalc_on_event,
        recalc_interval_minutes=defn.recalc_interval_minutes,
        created_by=current_user.id,
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)
    return _defn_to_response(db, clone)


# ============================================================================
# User scores
# ============================================================================

@router.get("/projects/{project_id}/users/{user_id}/scores")
def get_user_scores(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project(db, project_id, current_user)

    snapshots = (
        db.query(UserScoreSnapshot)
        .join(ScoreDefinition)
        .filter(
            UserScoreSnapshot.project_id == project_id,
            UserScoreSnapshot.user_id == user_id,
            ScoreDefinition.status == "active",
        )
        .all()
    )

    return [
        UserScoreResponse(
            score_definition_id=s.score_definition_id,
            slug=s.score_definition.slug,
            score_type=s.score_definition.score_type,
            name=s.score_definition.name,
            score=s.score,
            tier=s.tier,
            previous_score=s.previous_score,
            score_delta=s.score_delta,
            calculated_at=s.calculated_at,
        )
        for s in snapshots
    ]


@router.get("/scores/{definition_id}/users/{user_id}/explain")
def explain_user_score(
    definition_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)

    snapshot = db.query(UserScoreSnapshot).filter(
        UserScoreSnapshot.score_definition_id == definition_id,
        UserScoreSnapshot.user_id == user_id,
    ).first()

    if not snapshot:
        raise HTTPException(status_code=404, detail="No score for this user")

    items = []
    for item in (snapshot.explanation or []):
        items.append(ScoreExplanationItem(
            signal=item.get("signal", ""),
            event_name=item.get("event_name", ""),
            raw_value=item.get("raw_value", 0),
            weight=item.get("weight", 0),
            decay_factor=item.get("decay_factor", 1),
            weighted_value=item.get("weighted_value", 0),
            contribution_pct=item.get("contribution_pct", 0),
        ))

    return ScoreExplanationResponse(
        score=snapshot.score,
        tier=snapshot.tier,
        explanation=items,
        calculated_at=snapshot.calculated_at,
    )


# ============================================================================
# Analytics
# ============================================================================

@router.get("/scores/{definition_id}/distribution")
def get_distribution(
    definition_id: int,
    buckets: int = Query(10, ge=2, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)

    norm_max = defn.normalization_max or 100
    bucket_size = norm_max / buckets

    snapshots = db.query(UserScoreSnapshot.score).filter(
        UserScoreSnapshot.score_definition_id == definition_id,
    ).all()

    distribution = []
    for i in range(buckets):
        start = i * bucket_size
        end = start + bucket_size
        count = sum(1 for (s,) in snapshots if start <= s < end)
        if i == buckets - 1:
            count += sum(1 for (s,) in snapshots if s == end)
        distribution.append(ScoreDistributionBucket(
            bucket_start=round(start, 1),
            bucket_end=round(end, 1),
            count=count,
        ))

    return distribution


@router.get("/scores/{definition_id}/tier-counts")
def get_tier_counts(
    definition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)

    rows = (
        db.query(UserScoreSnapshot.tier, func.count(UserScoreSnapshot.id))
        .filter(UserScoreSnapshot.score_definition_id == definition_id)
        .group_by(UserScoreSnapshot.tier)
        .all()
    )

    counts = {"hot": 0, "warm": 0, "cold": 0}
    total = 0
    for tier, count in rows:
        counts[tier] = count
        total += count

    return TierCountResponse(
        hot=counts["hot"],
        warm=counts["warm"],
        cold=counts["cold"],
        total=total,
    )


@router.post("/scores/{definition_id}/recalculate")
def trigger_recalculate(
    definition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defn = _get_definition(db, definition_id)
    _get_project(db, defn.project_id, current_user)

    from app.services.scoring.scoring_engine import ScoringEngine
    engine = ScoringEngine(db)
    count = engine.batch_recalculate(definition_id)

    return {"detail": f"Recalculated {count} users", "count": count}
