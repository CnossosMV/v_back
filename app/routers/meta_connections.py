"""
Router for Meta page connections (Messenger + Instagram DMs/comments).

OAuth via Facebook Login for Business:
  start -> popup dialog -> backend callback (public) -> session authorized
  -> frontend polls session -> finalize with selected pages
  -> page tokens stored (Fernet) + app subscribed to each page.
"""

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import and_
import logging
import os
import secrets as secrets_mod

from app.database import get_db
from app import models
from app.dependencies import require_project_role
from app.schemas.meta_connections import (
    MetaOAuthStartResponse,
    MetaOAuthSessionResponse,
    MetaOAuthPageOption,
    MetaFinalizeRequest,
    MetaFinalizeResponse,
    MetaConnectionUpdateRequest,
    MetaConnectionResponse,
)
from app.services.meta_messaging_service import (
    meta_messaging_service,
    get_meta_app_config,
    PAGE_SUBSCRIBED_FIELDS,
)
from app.services.encryption_service import encrypt_value, decrypt_value

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta-connections"])

OAUTH_SESSION_TTL_MINUTES = 30


# ── Helpers ─────────────────────────────────────────────────────────────

def _require_meta_app_config():
    cfg = get_meta_app_config()
    missing = []
    if not cfg:
        missing = ["META_APP_ID", "META_APP_SECRET"]
    elif not cfg.get("config_id"):
        # Facebook Login for Business requires a configuration id
        missing = ["META_CONFIG_ID"]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Meta app not configured ({' / '.join(missing)} missing). Contact the administrator.",
        )
    return cfg


def _redirect_uri() -> str:
    backend_base = os.getenv("BACKEND_BASE_URL")
    if not backend_base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="BACKEND_BASE_URL not configured. Contact the administrator.",
        )
    return f"{backend_base.rstrip('/')}/api/v1/meta/oauth/callback"


def _find_conflicting_connection(
    db: Session, project_id: int, page_id: str, ig_account_id: str | None
):
    """Active connection for the same page/IG account in ANOTHER project.

    Ownership rule: one active connection per Page (and per IG account)
    globally — enforced here for all dialects and by partial unique
    indexes (129_meta_page_uq) on PostgreSQL.
    """
    filters = [models.MetaPageConnection.page_id == page_id]
    if ig_account_id:
        from sqlalchemy import or_

        filters = [
            or_(
                models.MetaPageConnection.page_id == page_id,
                models.MetaPageConnection.ig_account_id == ig_account_id,
            )
        ]
    return (
        db.query(models.MetaPageConnection)
        .filter(
            and_(
                *filters,
                models.MetaPageConnection.is_active == True,
                models.MetaPageConnection.project_id != project_id,
            )
        )
        .first()
    )


def _conflict_detail(db: Session, project_id: int, conflict) -> str:
    """409 message; names the other project only when it shares the caller's workspace."""
    own = db.query(models.Project).filter(models.Project.id == project_id).first()
    other = db.query(models.Project).filter(models.Project.id == conflict.project_id).first()
    if own and other and other.workspace_id == own.workspace_id:
        return f"This page is already connected to project '{other.name}'. Disconnect it there first."
    return "This page is already connected in another workspace."


def _get_project_connection(db: Session, project_id: int, connection_id: int) -> models.MetaPageConnection:
    conn = (
        db.query(models.MetaPageConnection)
        .filter(
            and_(
                models.MetaPageConnection.id == connection_id,
                models.MetaPageConnection.project_id == project_id,
            )
        )
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Meta connection not found")
    return conn


def _subscribed_fields_for(conn: models.MetaPageConnection) -> list:
    fields = [f for f in PAGE_SUBSCRIBED_FIELDS if f != "feed"]
    if conn.fb_comments_enabled:
        fields.append("feed")
    return fields


# ── OAuth flow ──────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/meta/oauth/start", response_model=MetaOAuthStartResponse)
async def start_meta_oauth(
    project_id: int,
    db: Session = Depends(get_db),
    membership=Depends(require_project_role("admin")),
):
    _require_meta_app_config()
    _redirect_uri()  # fail fast when BACKEND_BASE_URL is missing

    # Opportunistic purge: expire this project's stale pending sessions
    now = datetime.utcnow()
    stale = (
        db.query(models.MetaOAuthSession)
        .filter(
            and_(
                models.MetaOAuthSession.project_id == project_id,
                models.MetaOAuthSession.status.in_(["pending", "authorized"]),
                models.MetaOAuthSession.expires_at < now,
            )
        )
        .all()
    )
    for s in stale:
        s.status = "expired"
        s.user_token_enc = None
    if stale:
        db.commit()

    state = secrets_mod.token_urlsafe(32)
    session = models.MetaOAuthSession(
        state=state,
        project_id=project_id,
        user_id=membership["user_id"],
        status="pending",
        expires_at=datetime.utcnow() + timedelta(minutes=OAUTH_SESSION_TTL_MINUTES),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    auth_url = meta_messaging_service.build_oauth_dialog_url(_redirect_uri(), state)
    return MetaOAuthStartResponse(session_id=session.id, auth_url=auth_url)


@router.get("/meta/oauth/callback")
async def meta_oauth_callback(request: Request, db: Session = Depends(get_db)):
    """Public OAuth redirect target. Exchanges the code, lists pages, stores
    them on the session row and closes the popup (frontend polls the session)."""
    state = request.query_params.get("state", "")
    code = request.query_params.get("code")
    fb_error = request.query_params.get("error_description") or request.query_params.get("error")

    close_html = HTMLResponse(
        "<html><body><p>You can close this window.</p>"
        "<script>window.close();</script></body></html>"
    )

    session = (
        db.query(models.MetaOAuthSession)
        .filter(
            and_(
                models.MetaOAuthSession.state == state,
                models.MetaOAuthSession.status == "pending",
                models.MetaOAuthSession.expires_at > datetime.utcnow(),
            )
        )
        .first()
    )
    if not session:
        logger.warning("Meta OAuth callback with unknown/expired state")
        return close_html

    if fb_error or not code:
        session.status = "error"
        session.error = fb_error or "Authorization was cancelled"
        db.commit()
        return close_html

    try:
        short = await meta_messaging_service.exchange_code(code, _redirect_uri())
        if not short.get("success"):
            raise ValueError(short.get("error", "code exchange failed"))

        long_lived = await meta_messaging_service.exchange_long_lived(short["access_token"])
        if not long_lived.get("success"):
            raise ValueError(long_lived.get("error", "long-lived exchange failed"))
        user_token = long_lived["access_token"]

        pages_result = await meta_messaging_service.list_pages(user_token)
        if not pages_result.get("success"):
            raise ValueError(pages_result.get("error", "failed to list pages"))

        existing_page_ids = {
            row.page_id
            for row in db.query(models.MetaPageConnection.page_id)
            .filter(
                and_(
                    models.MetaPageConnection.project_id == session.project_id,
                    models.MetaPageConnection.is_active == True,
                )
            )
            .all()
        }

        # Metadata only — page access tokens are never stored on the session;
        # finalize re-fetches them with the encrypted user token.
        pages_json = []
        for p in pages_result["pages"]:
            pages_json.append(
                {
                    "page_id": p["page_id"],
                    "name": p["name"],
                    "ig_account_id": p["ig_account_id"],
                    "ig_username": p["ig_username"],
                    "already_connected": p["page_id"] in existing_page_ids,
                }
            )

        session.user_token_enc = encrypt_value(user_token)
        session.pages_json = pages_json
        session.status = "authorized"
        db.commit()
    except Exception as e:
        logger.error(f"Meta OAuth callback failed: {e}")
        session.status = "error"
        session.error = str(e)
        db.commit()

    return close_html


@router.get(
    "/projects/{project_id}/meta/oauth/sessions/{session_id}",
    response_model=MetaOAuthSessionResponse,
)
async def get_meta_oauth_session(
    project_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("admin")),
):
    session = (
        db.query(models.MetaOAuthSession)
        .filter(
            and_(
                models.MetaOAuthSession.id == session_id,
                models.MetaOAuthSession.project_id == project_id,
            )
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="OAuth session not found")

    pages = [
        MetaOAuthPageOption(
            page_id=p["page_id"],
            name=p.get("name"),
            ig_account_id=p.get("ig_account_id"),
            ig_username=p.get("ig_username"),
            already_connected=p.get("already_connected", False),
        )
        for p in (session.pages_json or [])
    ]
    return MetaOAuthSessionResponse(
        session_id=session.id, status=session.status, pages=pages, error=session.error
    )


@router.post(
    "/projects/{project_id}/meta/oauth/sessions/{session_id}/finalize",
    response_model=MetaFinalizeResponse,
)
async def finalize_meta_oauth(
    project_id: int,
    session_id: int,
    body: MetaFinalizeRequest,
    db: Session = Depends(get_db),
    membership=Depends(require_project_role("admin")),
):
    session = (
        db.query(models.MetaOAuthSession)
        .filter(
            and_(
                models.MetaOAuthSession.id == session_id,
                models.MetaOAuthSession.project_id == project_id,
                models.MetaOAuthSession.status == "authorized",
            )
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="OAuth session not found or not authorized")
    if session.expires_at < datetime.utcnow():
        session.user_token_enc = None
        session.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="OAuth session expired — reconnect with Facebook")

    if not session.user_token_enc:
        raise HTTPException(status_code=400, detail="OAuth session has no stored token — reconnect with Facebook")

    # Page tokens are not stored on the session — re-fetch them now with the
    # long-lived user token so they only ever live in meta_page_connections.
    user_token = decrypt_value(session.user_token_enc)
    pages_result = await meta_messaging_service.list_pages(user_token)
    if not pages_result.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch pages from Meta: {pages_result.get('error')}",
        )
    pages_by_id = {p["page_id"]: p for p in pages_result["pages"]}
    authorized_ids = {p["page_id"] for p in (session.pages_json or [])}

    connections = []
    errors = []

    for selection in body.pages:
        page = pages_by_id.get(selection.page_id)
        if not page or selection.page_id not in authorized_ids:
            errors.append(f"Page {selection.page_id} was not authorized in this session")
            continue

        conflict = _find_conflicting_connection(
            db, project_id, selection.page_id, page.get("ig_account_id")
        )
        if conflict:
            errors.append(
                f"{page.get('name') or selection.page_id}: {_conflict_detail(db, project_id, conflict)}"
            )
            continue

        instagram_enabled = selection.instagram_enabled and bool(page.get("ig_account_id"))
        ig_comments_enabled = selection.ig_comments_enabled and bool(page.get("ig_account_id"))

        conn = (
            db.query(models.MetaPageConnection)
            .filter(
                and_(
                    models.MetaPageConnection.project_id == project_id,
                    models.MetaPageConnection.page_id == selection.page_id,
                )
            )
            .first()
        )
        if not conn:
            conn = models.MetaPageConnection(project_id=project_id, page_id=selection.page_id)
            db.add(conn)

        conn.page_name = page.get("name")
        conn.page_access_token_enc = encrypt_value(page["access_token"])
        conn.ig_account_id = page.get("ig_account_id")
        conn.ig_username = page.get("ig_username")
        conn.messenger_enabled = selection.messenger_enabled
        conn.instagram_enabled = instagram_enabled
        conn.fb_comments_enabled = selection.fb_comments_enabled
        conn.ig_comments_enabled = ig_comments_enabled
        conn.connected_by_user_id = membership["user_id"]
        conn.is_active = True
        conn.status = "connected"
        conn.last_error = None
        db.commit()
        db.refresh(conn)

        fields = _subscribed_fields_for(conn)
        sub = await meta_messaging_service.subscribe_page(
            conn.page_id, page["access_token"], fields
        )
        if sub.get("success"):
            conn.subscribed_fields = fields
        else:
            conn.status = "error"
            conn.last_error = f"Webhook subscription failed: {sub.get('error')}"
            errors.append(f"{page.get('name') or selection.page_id}: {sub.get('error')}")
        db.commit()
        db.refresh(conn)
        connections.append(conn)

    # Purge the user token from the session (pages_json never held tokens)
    session.status = "completed"
    session.user_token_enc = None
    db.commit()

    return MetaFinalizeResponse(
        connections=[MetaConnectionResponse.model_validate(c) for c in connections],
        errors=errors,
    )


@router.post("/projects/{project_id}/meta/oauth/sessions/{session_id}/cancel")
async def cancel_meta_oauth(
    project_id: int,
    session_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("admin")),
):
    """Cancel an OAuth session and purge its credentials. Idempotent."""
    session = (
        db.query(models.MetaOAuthSession)
        .filter(
            and_(
                models.MetaOAuthSession.id == session_id,
                models.MetaOAuthSession.project_id == project_id,
            )
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="OAuth session not found")

    if session.status in ("pending", "authorized"):
        session.status = "cancelled"
    session.user_token_enc = None
    db.commit()
    return {"success": True}


# ── Connections CRUD ────────────────────────────────────────────────────

@router.get("/projects/{project_id}/meta/connections", response_model=list[MetaConnectionResponse])
async def list_meta_connections(
    project_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("viewer")),
):
    conns = (
        db.query(models.MetaPageConnection)
        .filter(
            and_(
                models.MetaPageConnection.project_id == project_id,
                models.MetaPageConnection.is_active == True,
            )
        )
        .order_by(models.MetaPageConnection.id.desc())
        .all()
    )
    return conns


@router.patch(
    "/projects/{project_id}/meta/connections/{connection_id}",
    response_model=MetaConnectionResponse,
)
async def update_meta_connection(
    project_id: int,
    connection_id: int,
    body: MetaConnectionUpdateRequest,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("admin")),
):
    conn = _get_project_connection(db, project_id, connection_id)

    comments_changed = False
    if body.messenger_enabled is not None:
        conn.messenger_enabled = body.messenger_enabled
    if body.instagram_enabled is not None:
        if body.instagram_enabled and not conn.ig_account_id:
            raise HTTPException(
                status_code=400,
                detail="This page has no linked Instagram professional account",
            )
        conn.instagram_enabled = body.instagram_enabled
    if body.fb_comments_enabled is not None and body.fb_comments_enabled != conn.fb_comments_enabled:
        conn.fb_comments_enabled = body.fb_comments_enabled
        comments_changed = True
    if body.ig_comments_enabled is not None:
        if body.ig_comments_enabled and not conn.ig_account_id:
            raise HTTPException(
                status_code=400,
                detail="This page has no linked Instagram professional account",
            )
        conn.ig_comments_enabled = body.ig_comments_enabled
    if body.is_active is not None:
        if body.is_active and not conn.is_active:
            conflict = _find_conflicting_connection(
                db, project_id, conn.page_id, conn.ig_account_id
            )
            if conflict:
                raise HTTPException(
                    status_code=409,
                    detail=_conflict_detail(db, project_id, conflict),
                )
        conn.is_active = body.is_active

    db.commit()
    db.refresh(conn)

    # Re-subscribe with/without `feed` when FB comment toggle flips
    if comments_changed and conn.page_access_token_enc:
        fields = _subscribed_fields_for(conn)
        sub = await meta_messaging_service.subscribe_page(
            conn.page_id, decrypt_value(conn.page_access_token_enc), fields
        )
        if sub.get("success"):
            conn.subscribed_fields = fields
            db.commit()
            db.refresh(conn)
        else:
            logger.warning(f"Meta re-subscribe failed for connection {conn.id}: {sub.get('error')}")

    return conn


@router.post(
    "/projects/{project_id}/meta/connections/{connection_id}/validate",
    response_model=MetaConnectionResponse,
)
async def validate_meta_connection(
    project_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("admin")),
):
    conn = _get_project_connection(db, project_id, connection_id)
    if not conn.page_access_token_enc:
        raise HTTPException(status_code=400, detail="Connection has no stored page token")

    result = await meta_messaging_service.validate_page_token(
        conn.page_id, decrypt_value(conn.page_access_token_enc)
    )
    if result.get("success"):
        conn.status = "connected"
        conn.last_error = None
        conn.page_name = result.get("page_name") or conn.page_name
        conn.ig_account_id = result.get("ig_account_id") or conn.ig_account_id
        conn.ig_username = result.get("ig_username") or conn.ig_username
    else:
        conn.status = "error"
        conn.last_error = result.get("error")
    db.commit()
    db.refresh(conn)
    return conn


@router.delete("/projects/{project_id}/meta/connections/{connection_id}")
async def delete_meta_connection(
    project_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    _membership=Depends(require_project_role("admin")),
):
    conn = _get_project_connection(db, project_id, connection_id)

    # Best-effort webhook unsubscribe (skip if another project still uses this page)
    other_active = (
        db.query(models.MetaPageConnection)
        .filter(
            and_(
                models.MetaPageConnection.page_id == conn.page_id,
                models.MetaPageConnection.id != conn.id,
                models.MetaPageConnection.is_active == True,
            )
        )
        .count()
    )
    if not other_active and conn.page_access_token_enc:
        try:
            await meta_messaging_service.unsubscribe_page(
                conn.page_id, decrypt_value(conn.page_access_token_enc)
            )
        except Exception as e:
            logger.warning(f"Meta unsubscribe failed for connection {conn.id}: {e}")

    conn.is_active = False
    conn.status = "disconnected"
    db.commit()
    return {"success": True}
