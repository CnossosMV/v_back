"""
One-click unsubscribe endpoint (RFC 8058).

Handles List-Unsubscribe-Post requests from email clients (Gmail, Yahoo, etc.)
and GET requests for manual unsubscribe links.
"""
import hashlib
import hmac
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.messaging import MessagingUser, MessagingEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["unsubscribe"])

_SECRET = os.getenv("JWT_SECRET", "default-secret-change-me")


def generate_unsubscribe_token(project_id: int, user_id: int, channel: str = "email") -> str:
    """Generate an HMAC-signed token encoding project_id, user_id, channel."""
    payload = f"{project_id}:{user_id}:{channel}"
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{project_id}.{user_id}.{channel}.{sig}"


def verify_unsubscribe_token(token: str) -> Optional[dict]:
    """Verify and decode an unsubscribe token. Returns {project_id, user_id, channel} or None."""
    parts = token.split(".")
    if len(parts) != 4:
        return None
    try:
        project_id = int(parts[0])
        user_id = int(parts[1])
        channel = parts[2]
        sig = parts[3]
    except (ValueError, IndexError):
        return None

    expected_payload = f"{project_id}:{user_id}:{channel}"
    expected_sig = hmac.new(_SECRET.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected_sig):
        return None

    return {"project_id": project_id, "user_id": user_id, "channel": channel}


@router.post("/unsubscribe/{token}")
async def one_click_unsubscribe(
    token: str,
    db: Session = Depends(get_db),
):
    """RFC 8058 one-click unsubscribe (POST). Called by email clients."""
    data = verify_unsubscribe_token(token)
    if not data:
        raise HTTPException(status_code=400, detail="Invalid unsubscribe token")

    _process_unsubscribe(db, data["project_id"], data["user_id"], data["channel"])
    return {"status": "unsubscribed"}


@router.get("/unsubscribe/{token}")
async def unsubscribe_page(
    token: str,
    db: Session = Depends(get_db),
):
    """GET unsubscribe page — shows confirmation and processes unsubscribe."""
    data = verify_unsubscribe_token(token)
    if not data:
        return HTMLResponse(
            "<html><body><h2>Invalid or expired link</h2></body></html>",
            status_code=400,
        )

    _process_unsubscribe(db, data["project_id"], data["user_id"], data["channel"])

    return HTMLResponse("""
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="font-family: -apple-system, sans-serif; text-align: center; padding: 60px 20px;">
        <h2>You have been unsubscribed</h2>
        <p style="color: #666;">You will no longer receive messages on this channel.</p>
    </body>
    </html>
    """)


def _process_unsubscribe(db: Session, project_id: int, user_id: int, channel: str) -> None:
    """Process the unsubscribe: opt out channel + emit event."""
    user = db.query(MessagingUser).filter(
        MessagingUser.id == user_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not user:
        return

    channels = list(user.opted_out_channels or [])
    if channel not in channels:
        channels.append(channel)
        user.opted_out_channels = channels

    # Emit event
    event = MessagingEvent(
        project_id=project_id,
        user_id=user_id,
        event_name="contact.unsubscribed",
        properties={"channel": channel, "method": "one_click"},
        source="system",
        processed=False,
    )
    db.add(event)
    db.commit()

    logger.info("User %s unsubscribed from %s (project %s)", user_id, channel, project_id)
