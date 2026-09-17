"""
App-level Meta webhook for Messenger + Instagram (DMs and comments).

Unlike WhatsApp Cloud (per-instance webhook URLs), the `page` and
`instagram` webhook objects deliver to ONE app-level callback URL.
Events are dispatched by payload["object"] + entry["id"] (page id /
IG account id) to exactly ONE active MetaPageConnection — ownership
rule: one active connection per Page / IG account globally (enforced
by 129_meta_page_uq partial unique indexes + app-level checks).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
import json
import logging
import os

from app.database import get_db
from app import models
from app.services.meta_cloud_api_service import meta_cloud_api_service
from app.services.meta_messaging_service import meta_messaging_service
from app.services.meta_window_service import MetaWindowService
from app.services.encryption_service import decrypt_value_or_none

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meta/webhook", tags=["meta-webhooks"])


# ── Verification handshake ──────────────────────────────────────────────

@router.get("")
async def meta_webhook_verify(request: Request):
    """Meta webhook verification (GET) — returns hub.challenge."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    verify_token = os.getenv("META_WEBHOOK_VERIFY_TOKEN")
    if mode != "subscribe" or not verify_token:
        raise HTTPException(status_code=403, detail="Invalid mode")
    if token != verify_token:
        raise HTTPException(status_code=403, detail="Invalid verify token")

    return int(challenge) if challenge and challenge.isdigit() else challenge


# ── Event delivery ──────────────────────────────────────────────────────

@router.post("")
async def meta_webhook_receive(request: Request, db: Session = Depends(get_db)):
    """Receive page/instagram webhook events (messages, postbacks, comments)."""
    body_bytes = await request.body()
    sig_header = request.headers.get("X-Hub-Signature-256", "")

    app_secret = os.getenv("META_APP_SECRET")
    if not app_secret:
        logger.error("META_APP_SECRET not configured — rejecting Meta webhook")
        raise HTTPException(status_code=503, detail="Meta app not configured")
    if not meta_cloud_api_service.verify_webhook_signature(body_bytes, sig_header, app_secret):
        logger.warning("Invalid Meta webhook signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body_bytes)
    obj = payload.get("object", "")

    if obj not in ("page", "instagram"):
        logger.info(f"Ignoring Meta webhook object={obj}")
        return {"status": "ok"}

    channel = "messenger" if obj == "page" else "instagram"

    for entry in payload.get("entry", []):
        entry_id = str(entry.get("id", ""))
        if not entry_id:
            continue

        connection = _find_connection(db, channel, entry_id)
        if not connection:
            logger.info(f"Meta webhook: no active connection for {obj} id={entry_id}")
            continue

        # DM / postback / read events
        for event in entry.get("messaging", []):
            try:
                await _process_messaging_event(db, connection, channel, entry_id, event)
            except Exception as e:
                logger.error(f"Error processing {channel} event: {e}", exc_info=True)

        # Comment events (FB `feed`, IG `comments`)
        for change in entry.get("changes", []):
            try:
                await _process_change_event(db, connection, channel, entry_id, change)
            except Exception as e:
                logger.error(f"Error processing {channel} change: {e}", exc_info=True)

    # Always 200 — Meta retries (and eventually disables) failing webhooks
    return {"status": "ok"}


def _find_connection(db: Session, channel: str, entry_id: str):
    """The single active connection owning a webhook entry id.

    Ownership rule: one active connection per Page / IG account globally.
    If duplicates somehow exist (index not yet applied), route to the newest
    and log loudly — never fan out to multiple projects.
    """
    q = db.query(models.MetaPageConnection).filter(
        models.MetaPageConnection.is_active == True,
    )
    if channel == "messenger":
        q = q.filter(models.MetaPageConnection.page_id == entry_id)
    else:
        q = q.filter(models.MetaPageConnection.ig_account_id == entry_id)
    rows = q.order_by(models.MetaPageConnection.id.desc()).limit(2).all()
    if len(rows) > 1:
        logger.error(
            f"META_WH multiple active connections for {channel} id={entry_id} — "
            f"routing to newest (connection {rows[0].id}); run 129_meta_page_uq"
        )
    return rows[0] if rows else None


async def _process_messaging_event(
    db: Session,
    connection: models.MetaPageConnection,
    channel: str,
    entry_id: str,
    event: dict,
):
    sender_id = (event.get("sender") or {}).get("id", "")

    # Skip echoes of our own sends and any event authored by the page/IG account
    if (event.get("message") or {}).get("is_echo"):
        return
    if not sender_id or sender_id == entry_id or sender_id == connection.page_id:
        return
    if channel == "instagram" and sender_id == connection.ig_account_id:
        return

    # Read receipts carry a watermark (not a message id) — no per-message
    # delivery mapping is possible; skip.
    if event.get("read") or event.get("messaging_seen"):
        return

    # Platform toggle
    if channel == "messenger" and not connection.messenger_enabled:
        return
    if channel == "instagram" and not connection.instagram_enabled:
        return

    # Best-effort profile name
    push_name = None
    page_token = decrypt_value_or_none(connection.page_access_token_enc)
    if page_token:
        try:
            if channel == "messenger":
                profile = await meta_messaging_service.get_messenger_profile(sender_id, page_token)
            else:
                profile = await meta_messaging_service.get_ig_profile(sender_id, page_token)
            if profile:
                push_name = profile.get("name")
        except Exception:
            pass

    from app.services.inbound.adapters import normalize_meta_messaging
    from app.services.inbound.router import InboundRouter

    inbound_msg = normalize_meta_messaging(
        event,
        channel=channel,
        project_id=connection.project_id,
        instance_id=connection.id,
        instance_name=connection.page_name,
        push_name=push_name,
        channel_context={
            "page_id": connection.page_id,
            "ig_account_id": connection.ig_account_id,
            "platform": channel,
        },
    )
    if not inbound_msg:
        return

    # Open/refresh the 24h messaging window
    MetaWindowService(db).update_window(connection.id, channel, sender_id)

    logger.warning(
        f"[META_MSG_WH] Inbound {channel} message from={sender_id} "
        f"connection={connection.id} project={connection.project_id}"
    )
    result = await InboundRouter(db).route(inbound_msg)
    logger.warning(f"[META_MSG_WH] Route result: {result}")


async def _process_change_event(
    db: Session,
    connection: models.MetaPageConnection,
    channel: str,
    entry_id: str,
    change: dict,
):
    field = change.get("field", "")
    value = change.get("value", {}) or {}

    if channel == "messenger" and field == "feed":
        if not connection.fb_comments_enabled:
            return
        if value.get("item") != "comment":
            return
        from app.services.meta_comment_service import MetaCommentService
        await MetaCommentService(db).process_fb_comment(connection, value)

    elif channel == "instagram" and field == "comments":
        if not connection.ig_comments_enabled:
            return
        from app.services.meta_comment_service import MetaCommentService
        await MetaCommentService(db).process_ig_comment(connection, value)
