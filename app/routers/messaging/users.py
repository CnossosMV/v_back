"""
Messaging Users Router
View and manage identified users/contacts
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

from app.database import get_db
from app.models import Project, User
from app.models.messaging import (
    ContactVerificationState,
    MessagingUser,
    MessagingAnonymousProfile,
    MessagingEvent,
)
from app.schemas.messaging import MessagingUserResponse, PauseAutomationsRequest, SendWindowsUpdateRequest
from app.routers.auth import get_current_user

router = APIRouter(prefix="/projects/{project_id}/messaging/users", tags=["messaging-users"])


def _touch_from_event(event: MessagingEvent) -> Optional[Dict[str, Any]]:
    attr = event.attribution if isinstance(event.attribution, dict) else {}
    if not attr:
        return None
    utm = attr.get("utm") if isinstance(attr.get("utm"), dict) else {}
    click_ids = attr.get("click_ids") if isinstance(attr.get("click_ids"), dict) else {}
    page = attr.get("page") if isinstance(attr.get("page"), dict) else {}
    cookies = attr.get("cookies") if isinstance(attr.get("cookies"), dict) else {}
    return {
        "event_id": event.id,
        "external_event_id": event.external_event_id,
        "event_name": event.event_name,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "campaign_origin": event.campaign_origin or attr.get("campaign_origin") or "unknown",
        "utm": utm,
        "click_ids": click_ids,
        "page": page,
        "cookies_present": {key: bool(value) for key, value in cookies.items()},
        "session_id": attr.get("session_id") or event.session_id,
        "source": event.source,
    }


def _touch_from_profile(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    attr = value.get("attribution") if isinstance(value.get("attribution"), dict) else {}
    utm = attr.get("utm") if isinstance(attr.get("utm"), dict) else {}
    click_ids = attr.get("click_ids") if isinstance(attr.get("click_ids"), dict) else {}
    page = attr.get("page") if isinstance(attr.get("page"), dict) else {}
    cookies = attr.get("cookies") if isinstance(attr.get("cookies"), dict) else {}
    return {
        "event_id": value.get("event_id"),
        "event_name": value.get("event_name"),
        "created_at": value.get("at"),
        "campaign_origin": value.get("campaign_origin") or attr.get("campaign_origin") or "unknown",
        "utm": utm,
        "click_ids": click_ids,
        "page": page,
        "cookies_present": {key: bool(v) for key, v in cookies.items()},
        "session_id": attr.get("session_id"),
        "source": "profile",
    }


def get_project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    """Get project and verify workspace access"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _verification_map(db: Session, project_id: int, user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not user_ids:
        return {}
    current_hashes = {
        row.id: {"email": row.email_hash, "whatsapp": row.phone_hash}
        for row in db.query(
            MessagingUser.id,
            MessagingUser.email_hash,
            MessagingUser.phone_hash,
        ).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.id.in_(user_ids),
        ).all()
    }
    rows = db.query(ContactVerificationState).filter(
        ContactVerificationState.project_id == project_id,
        ContactVerificationState.user_id.in_(user_ids),
    ).order_by(
        ContactVerificationState.checked_at.asc(),
        ContactVerificationState.id.asc(),
    ).all()
    now = datetime.utcnow()
    result: Dict[int, Dict[str, Any]] = {}
    for state in rows:
        expected_hash = current_hashes.get(state.user_id, {}).get(state.verification_type)
        if not expected_hash or state.identifier_hash != expected_hash:
            continue
        result.setdefault(state.user_id, {})[state.verification_type] = {
            "status": state.canonical_status,
            "provider": state.provider,
            "provider_key_source": state.provider_key_source,
            "provider_version": state.provider_version,
            "provider_status": state.provider_status,
            "checked_at": state.checked_at,
            "expires_at": state.expires_at,
            "is_expired": state.expires_at <= now,
            "last_attempt_status": state.last_attempt_status,
            "last_error_code": state.last_error_code,
        }
    return result


def _apply_user_filters(
    query,
    db: Session,
    project_id: int,
    *,
    search: Optional[str] = None,
    is_subscribed: Optional[bool] = None,
    segment_rule_id: Optional[int] = None,
    tag: Optional[str] = None,
    email_verification_status: Optional[str] = None,
    whatsapp_verification_status: Optional[str] = None,
    verification_older_than_days: Optional[int] = None,
    verification_age_type: str = "email",
):
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                MessagingUser.email.ilike(search_term),
                MessagingUser.name.ilike(search_term),
                MessagingUser.external_id.ilike(search_term),
                MessagingUser.phone.ilike(search_term),
            )
        )
    if is_subscribed is not None:
        query = query.filter(MessagingUser.is_subscribed == is_subscribed)
    if segment_rule_id is not None:
        query = query.filter(MessagingUser.segment_rule_id == segment_rule_id)
    if tag:
        from sqlalchemy import text
        query = query.filter(text("tags::jsonb ? :tag_val").bindparams(tag_val=tag))

    for verification_type, verification_status, current_hash in (
        ("email", email_verification_status, MessagingUser.email_hash),
        ("whatsapp", whatsapp_verification_status, MessagingUser.phone_hash),
    ):
        if not verification_status:
            continue
        query = query.filter(current_hash.isnot(None))
        state_query = db.query(ContactVerificationState.id).filter(
            ContactVerificationState.project_id == project_id,
            ContactVerificationState.user_id == MessagingUser.id,
            ContactVerificationState.verification_type == verification_type,
            ContactVerificationState.identifier_hash == current_hash,
        )
        if verification_status == "unverified":
            query = query.filter(~state_query.exists())
        else:
            query = query.filter(
                state_query.filter(
                    ContactVerificationState.canonical_status == verification_status,
                ).exists(),
            )

    if verification_older_than_days is not None:
        cutoff = datetime.utcnow() - timedelta(days=verification_older_than_days)
        current_hash = (
            MessagingUser.email_hash
            if verification_age_type == "email"
            else MessagingUser.phone_hash
        )
        query = query.filter(
            current_hash.isnot(None),
            db.query(ContactVerificationState.id).filter(
                ContactVerificationState.project_id == project_id,
                ContactVerificationState.user_id == MessagingUser.id,
                ContactVerificationState.verification_type == verification_age_type,
                ContactVerificationState.identifier_hash == current_hash,
                ContactVerificationState.checked_at <= cutoff,
            ).exists(),
        )
    return query


@router.get("/tags/inventory")
def list_tags(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all tags used in the project with counts"""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from sqlalchemy import text
    result = db.execute(
        text("""
            SELECT tag, COUNT(*) as count
            FROM messaging_users,
                 json_array_elements_text(tags) AS tag
            WHERE project_id = :project_id
              AND tags IS NOT NULL
              AND status = 'active'
            GROUP BY tag
            ORDER BY tag
        """),
        {"project_id": project_id}
    ).fetchall()
    return [{"tag": row[0], "count": row[1]} for row in result]


@router.get("/", response_model=List[MessagingUserResponse])
def list_users(
    project_id: int,
    search: Optional[str] = Query(None, description="Search by email, name, or external_id"),
    is_subscribed: Optional[bool] = Query(None, description="Filter by subscription status"),
    segment_rule_id: Optional[int] = Query(None, description="Filter by segment rule ID"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    email_verification_status: Optional[str] = Query(
        None, pattern="^(valid|invalid|risky|unverified)$",
    ),
    whatsapp_verification_status: Optional[str] = Query(
        None, pattern="^(valid|invalid|risky|unverified)$",
    ),
    verification_older_than_days: Optional[int] = Query(None, ge=1, le=3650),
    verification_age_type: str = Query("email", pattern="^(email|whatsapp)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List messaging users with optional filters"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id
    )

    query = _apply_user_filters(
        query,
        db,
        project_id,
        search=search,
        is_subscribed=is_subscribed,
        segment_rule_id=segment_rule_id,
        tag=tag,
        email_verification_status=email_verification_status,
        whatsapp_verification_status=whatsapp_verification_status,
        verification_older_than_days=verification_older_than_days,
        verification_age_type=verification_age_type,
    )

    # Order by most recently seen
    query = query.order_by(MessagingUser.last_seen_at.desc())

    # Paginate
    users = query.offset(skip).limit(limit).all()

    # Enrich with thermal_state from journey_snapshots
    thermal_map = {}
    if users:
        from app.models.journey import JourneySnapshot
        user_ids = [u.id for u in users]
        snapshots = db.query(
            JourneySnapshot.user_id, JourneySnapshot.thermal_state
        ).filter(
            JourneySnapshot.project_id == project_id,
            JourneySnapshot.user_id.in_(user_ids),
        ).all()
        thermal_map = {s.user_id: s.thermal_state for s in snapshots}

    verification_map = _verification_map(db, project_id, [u.id for u in users])

    # Build response with thermal_state and verification snapshots injected
    from app.schemas.messaging import MessagingUserResponse
    results = []
    for u in users:
        user_dict = MessagingUserResponse.model_validate(u).model_dump()
        user_dict['thermal_state'] = thermal_map.get(u.id)
        user_dict['verifications'] = verification_map.get(u.id, {})
        results.append(user_dict)

    return results


# ── Automation pause endpoints ───────────────────────────────────────────
# IMPORTANT: GET /paused must be declared BEFORE /{user_id} so Starlette
# doesn't try to coerce "paused" into an int and return 422.


@router.get("/paused")
def list_paused_contacts(
    project_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated list of contacts with automations paused."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.services.messaging.contact_pause_service import ContactPauseService
    return ContactPauseService(db).get_paused_contacts(
        project_id, page=page, page_size=page_size, search=search,
    )


@router.patch("/{user_id}/pause-automations", response_model=MessagingUserResponse)
def pause_user_automations(
    project_id: int,
    user_id: int,
    payload: PauseAutomationsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pause automated messages for a contact (manual sends unaffected)."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.services.messaging.contact_pause_service import ContactPauseService
    try:
        return ContactPauseService(db).pause_contact(
            project_id, user_id, payload.mode, payload.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{user_id}/resume-automations", response_model=MessagingUserResponse)
def resume_user_automations(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Release a contact back to automations."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.services.messaging.contact_pause_service import ContactPauseService
    try:
        return ContactPauseService(db).resume_contact(project_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{user_id}/send-windows", response_model=MessagingUserResponse)
def update_user_send_windows(
    project_id: int,
    user_id: int,
    payload: SendWindowsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the contact's best-time-to-send override (null = inherit project)."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.send_windows = payload.send_windows
    db.commit()
    db.refresh(user)
    user_dict = MessagingUserResponse.model_validate(user).model_dump()
    user_dict["verifications"] = _verification_map(db, project_id, [user.id]).get(user.id, {})
    return user_dict


# ── Opted-out contacts ───────────────────────────────────────────────────
# Consolidated view of every unsubscribe flavor: is_subscribed=false (manual
# toggle), global_opt_out (STOP keyword / unsubscribe link), and per-channel
# opted_out_channels. Declared BEFORE /{user_id} (int coercion would 422).


@router.get("/opted-out")
def list_opted_out_contacts(
    project_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated list of contacts that unsubscribed or opted out on any channel."""
    from sqlalchemy import func, cast, String as SAString
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.status != "merged",
        or_(
            MessagingUser.is_subscribed == False,  # noqa: E712
            MessagingUser.global_opt_out == True,  # noqa: E712
            func.coalesce(cast(MessagingUser.opted_out_channels, SAString), "[]").notin_(("[]", "null")),
        ),
    )

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                MessagingUser.name.ilike(search_term),
                MessagingUser.external_id.ilike(search_term),
                MessagingUser.phone.ilike(search_term),
                MessagingUser.email.ilike(search_term),
            )
        )

    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    users = query.order_by(MessagingUser.updated_at.desc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()

    return {
        "contacts": [
            {
                "id": u.id,
                "project_id": u.project_id,
                "external_id": u.external_id,
                "name": u.name,
                "phone": u.phone,
                "email": u.email,
                "is_subscribed": u.is_subscribed,
                "global_opt_out": u.global_opt_out,
                "opted_out_channels": u.opted_out_channels or [],
                "updated_at": u.updated_at.isoformat() if u.updated_at else None,
            }
            for u in users
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.patch("/{user_id}/restore-subscription", response_model=MessagingUserResponse)
def restore_subscription(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fully restore a contact's subscription: re-subscribe, clear global
    opt-out and all per-channel opt-outs. Explicit operator action — the UI
    confirms before calling (the contact may have opted out via STOP)."""
    get_project_or_404(db, project_id, current_user.workspace_id)

    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_subscribed = True
    user.global_opt_out = False
    user.opted_out_channels = []
    db.commit()
    db.refresh(user)
    user_dict = MessagingUserResponse.model_validate(user).model_dump()
    user_dict["verifications"] = _verification_map(db, project_id, [user.id]).get(user.id, {})
    return user_dict


# ── Anonymous Visitor endpoints ──────────────────────────────────────────
# IMPORTANT: must be declared BEFORE /{user_id} so Starlette doesn't try to
# coerce "anonymous" into an int and return 422.


@router.get("/anonymous/count")
def count_anonymous_visitors(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Count unmerged anonymous visitor profiles."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    count = (
        db.query(func.count(MessagingAnonymousProfile.id))
        .filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.merged_to_user_id.is_(None),
        )
        .scalar()
    )
    return {"count": count or 0}


@router.get("/anonymous")
def list_anonymous_visitors(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List unmerged anonymous visitor profiles with event counts."""
    get_project_or_404(db, project_id, current_user.workspace_id)

    event_count_sq = (
        db.query(
            MessagingEvent.anonymous_id,
            func.count(MessagingEvent.id).label("event_count"),
        )
        .filter(MessagingEvent.project_id == project_id)
        .group_by(MessagingEvent.anonymous_id)
        .subquery()
    )

    page_count_sq = (
        db.query(
            MessagingEvent.anonymous_id,
            func.count(MessagingEvent.id).label("page_view_count"),
        )
        .filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.event_name == "page",
        )
        .group_by(MessagingEvent.anonymous_id)
        .subquery()
    )

    rows = (
        db.query(
            MessagingAnonymousProfile,
            func.coalesce(event_count_sq.c.event_count, 0).label("event_count"),
            func.coalesce(page_count_sq.c.page_view_count, 0).label("page_view_count"),
        )
        .outerjoin(
            event_count_sq,
            event_count_sq.c.anonymous_id == MessagingAnonymousProfile.anonymous_id,
        )
        .outerjoin(
            page_count_sq,
            page_count_sq.c.anonymous_id == MessagingAnonymousProfile.anonymous_id,
        )
        .filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.merged_to_user_id.is_(None),
        )
        .order_by(MessagingAnonymousProfile.last_seen_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        {
            "id": profile.id,
            "anonymous_id": profile.anonymous_id,
            "first_seen_at": profile.first_seen_at.isoformat() if profile.first_seen_at else None,
            "last_seen_at": profile.last_seen_at.isoformat() if profile.last_seen_at else None,
            "event_count": ev_count,
            "page_view_count": pv_count,
            "properties": profile.properties or {},
            "device_fingerprint": profile.device_fingerprint,
        }
        for profile, ev_count, pv_count in rows
    ]


@router.get("/anonymous/{anonymous_id}")
def get_anonymous_visitor(
    project_id: int,
    anonymous_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Single anonymous visitor profile enriched with event_count and page_view_count."""
    get_project_or_404(db, project_id, current_user.workspace_id)

    profile = (
        db.query(MessagingAnonymousProfile)
        .filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == anonymous_id,
        )
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Anonymous visitor not found")

    event_count = (
        db.query(func.count(MessagingEvent.id))
        .filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.anonymous_id == anonymous_id,
        )
        .scalar()
        or 0
    )
    page_view_count = (
        db.query(func.count(MessagingEvent.id))
        .filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.anonymous_id == anonymous_id,
            MessagingEvent.event_name == "page",
        )
        .scalar()
        or 0
    )

    return {
        "id": profile.id,
        "anonymous_id": profile.anonymous_id,
        "first_seen_at": profile.first_seen_at.isoformat() if profile.first_seen_at else None,
        "last_seen_at": profile.last_seen_at.isoformat() if profile.last_seen_at else None,
        "event_count": event_count,
        "page_view_count": page_view_count,
        "properties": profile.properties or {},
        "device_fingerprint": profile.device_fingerprint,
        "merged_to_user_id": profile.merged_to_user_id,
        "merged_at": profile.merged_at.isoformat() if profile.merged_at else None,
    }


@router.get("/anonymous/{anonymous_id}/events")
def get_anonymous_visitor_events(
    project_id: int,
    anonymous_id: str,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the event timeline for an anonymous visitor."""
    get_project_or_404(db, project_id, current_user.workspace_id)

    events = (
        db.query(MessagingEvent)
        .filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.anonymous_id == anonymous_id,
        )
        .order_by(MessagingEvent.created_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": e.id,
            "event_name": e.event_name,
            "properties": e.properties or {},
            "ip_address": e.ip_address,
            "user_agent": e.user_agent,
            "session_id": e.session_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


@router.get("/{user_id}", response_model=MessagingUserResponse)
def get_user(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific messaging user"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_dict = MessagingUserResponse.model_validate(user).model_dump()
    user_dict["verifications"] = _verification_map(db, project_id, [user.id]).get(user.id, {})
    return user_dict


@router.get("/{user_id}/acquisition-history")
def get_user_acquisition_history(
    project_id: int,
    user_id: int,
    limit: int = Query(100, ge=1, le=300),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Summarize where a user came from and which campaign touches are known."""
    get_project_or_404(db, project_id, current_user.workspace_id)

    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    attributed_events = (
        db.query(MessagingEvent)
        .filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.user_id == user_id,
            MessagingEvent.attribution.isnot(None),
        )
        .order_by(MessagingEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    attributed_events.reverse()

    touches = [touch for touch in (_touch_from_event(e) for e in attributed_events) if touch]
    origins: Dict[str, int] = {}
    campaigns: Dict[str, int] = {}
    for touch in touches:
        origin = touch.get("campaign_origin") or "unknown"
        origins[origin] = origins.get(origin, 0) + 1
        utm = touch.get("utm") or {}
        campaign = utm.get("utm_campaign")
        if campaign:
            campaigns[campaign] = campaigns.get(campaign, 0) + 1

    props = user.properties or {}
    first_touch = _touch_from_profile(props.get("first_touch_attribution"))
    last_touch = _touch_from_profile(props.get("last_touch_attribution"))
    if not first_touch and touches:
        first_touch = touches[0]
    if not last_touch and touches:
        last_touch = touches[-1]

    outreach_rows = (
        db.query(MessagingEvent.event_name, func.count(MessagingEvent.id).label("count"))
        .filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.user_id == user_id,
            or_(
                MessagingEvent.event_name.ilike("channel.%"),
                MessagingEvent.event_name.ilike("message.%"),
                MessagingEvent.event_name.ilike("email.%"),
                MessagingEvent.event_name.ilike("whatsapp.%"),
            ),
        )
        .group_by(MessagingEvent.event_name)
        .order_by(func.count(MessagingEvent.id).desc())
        .all()
    )

    return {
        "user_id": user_id,
        "first_touch": first_touch,
        "last_touch": last_touch,
        "touches": touches,
        "origin_counts": origins,
        "campaign_counts": campaigns,
        "outreach_reactions": [
            {"event_name": event_name, "count": count}
            for event_name, count in outreach_rows
        ],
    }


@router.get("/by-external/{external_id}", response_model=MessagingUserResponse)
def get_user_by_external_id(
    project_id: int,
    external_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a messaging user by their external ID"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    user = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.external_id == external_id
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_dict = MessagingUserResponse.model_validate(user).model_dump()
    user_dict["verifications"] = _verification_map(db, project_id, [user.id]).get(user.id, {})
    return user_dict


@router.patch("/{user_id}/unsubscribe", response_model=MessagingUserResponse)
def unsubscribe_user(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Unsubscribe a user from messaging"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_subscribed = False
    db.commit()
    db.refresh(user)

    return user


@router.patch("/{user_id}/subscribe", response_model=MessagingUserResponse)
def subscribe_user(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Resubscribe a user to messaging"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_subscribed = True
    db.commit()
    db.refresh(user)

    return user


@router.get("/count/total")
def count_users(
    project_id: int,
    search: Optional[str] = Query(None),
    is_subscribed: Optional[bool] = Query(None),
    segment_rule_id: Optional[int] = Query(None),
    tag: Optional[str] = Query(None),
    email_verification_status: Optional[str] = Query(
        None, pattern="^(valid|invalid|risky|unverified)$",
    ),
    whatsapp_verification_status: Optional[str] = Query(
        None, pattern="^(valid|invalid|risky|unverified)$",
    ),
    verification_older_than_days: Optional[int] = Query(None, ge=1, le=3650),
    verification_age_type: str = Query("email", pattern="^(email|whatsapp)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get total count of messaging users"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    query = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id
    )

    query = _apply_user_filters(
        query,
        db,
        project_id,
        search=search,
        is_subscribed=is_subscribed,
        segment_rule_id=segment_rule_id,
        tag=tag,
        email_verification_status=email_verification_status,
        whatsapp_verification_status=whatsapp_verification_status,
        verification_older_than_days=verification_older_than_days,
        verification_age_type=verification_age_type,
    )

    return {"count": query.count()}


# ── User 360 profile endpoints ─────────────────────────────────────────


@router.get("/{user_id}/enrollments")
def get_user_enrollments(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all funnel enrollments for a user across all funnels."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.models import FunnelEnrollment, Funnel

    rows = (
        db.query(FunnelEnrollment, Funnel.name.label("funnel_name"))
        .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
        .filter(
            FunnelEnrollment.user_id == user_id,
            Funnel.project_id == project_id,
        )
        .order_by(FunnelEnrollment.enrolled_at.desc())
        .all()
    )
    return [
        {
            "id": enr.id,
            "funnel_id": enr.funnel_id,
            "funnel_name": fname,
            "status": enr.status,
            "current_step_id": enr.current_step_id,
            "messages_sent": enr.messages_sent,
            "exit_reason": enr.exit_reason,
            "enrolled_at": enr.enrolled_at.isoformat() if enr.enrolled_at else None,
            "exited_at": enr.exited_at.isoformat() if enr.exited_at else None,
        }
        for enr, fname in rows
    ]


@router.get("/{user_id}/delivery-feedback")
def get_user_delivery_feedback(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all delivery feedback records for a user."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.models import DeliveryFeedback

    items = (
        db.query(DeliveryFeedback)
        .filter(
            DeliveryFeedback.project_id == project_id,
            DeliveryFeedback.user_id == user_id,
        )
        .order_by(DeliveryFeedback.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": f.id,
            "channel": f.channel,
            "recipient": f.recipient,
            "feedback_type": f.feedback_type,
            "reason": f.reason,
            "provider": f.provider,
            "provider_code": f.provider_code,
            "action_taken": f.action_taken,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in items
    ]


@router.get("/{user_id}/reachability")
def get_user_reachability(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get per-channel reachability status for a user."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    from app.models import DeliveryFeedback

    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    opted_out = set(user.opted_out_channels or [])
    global_out = user.global_opt_out or False

    # Get latest feedback per channel
    from sqlalchemy import func
    latest_feedback = (
        db.query(
            DeliveryFeedback.channel,
            DeliveryFeedback.feedback_type,
            func.max(DeliveryFeedback.created_at).label("last_at"),
        )
        .filter(
            DeliveryFeedback.project_id == project_id,
            DeliveryFeedback.user_id == user_id,
        )
        .group_by(DeliveryFeedback.channel, DeliveryFeedback.feedback_type)
        .all()
    )

    feedback_map: dict = {}
    for ch, ftype, last_at in latest_feedback:
        if ch not in feedback_map or last_at > feedback_map[ch]["last_at"]:
            feedback_map[ch] = {"feedback_type": ftype, "last_at": last_at}

    # Build reachability per channel
    channels_to_check = {"email", "whatsapp", "sms"} | opted_out | set(feedback_map.keys())
    reachability = []
    for ch in sorted(channels_to_check):
        if global_out:
            status = "global_opted_out"
        elif ch in opted_out:
            fb = feedback_map.get(ch)
            if fb and fb["feedback_type"] in ("hard_bounce", "unreachable"):
                status = "bounced"
            elif fb and fb["feedback_type"] == "complaint":
                status = "complained"
            elif fb and fb["feedback_type"] == "blocked":
                status = "blocked"
            else:
                status = "opted_out"
        elif ch in feedback_map:
            fb = feedback_map[ch]
            if fb["feedback_type"] == "soft_bounce":
                status = "soft_bouncing"
            else:
                status = "reachable"
        else:
            status = "reachable"

        reachability.append({
            "channel": ch,
            "status": status,
            "last_feedback": feedback_map.get(ch, {}).get("feedback_type"),
            "last_feedback_at": feedback_map[ch]["last_at"].isoformat() if ch in feedback_map else None,
        })

    return {
        "user_id": user_id,
        "global_opt_out": global_out,
        "channels": reachability,
    }
