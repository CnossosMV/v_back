"""
Email Tracking — public endpoints for open + click tracking.

GET /t/o/{token}.gif      — open pixel (no auth)
GET /t/c/{token}/{index}  — click redirect (no auth)
"""
import logging
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.email_tracking_service import EmailTrackingService, PIXEL_GIF

logger = logging.getLogger(__name__)

router = APIRouter(tags=["email-tracking"])


@router.get("/t/o/{token}.gif")
def track_email_open(token: str, request: Request, db: Session = Depends(get_db)):
    """
    Tracking pixel endpoint — returns 1x1 transparent GIF.
    Always returns 200 to avoid leaking information about token validity.
    """
    try:
        user_agent = request.headers.get("user-agent", "")
        svc = EmailTrackingService()
        svc.record_open(db, token, user_agent)
    except Exception:
        pass  # Silently ignore errors — always return the pixel

    return Response(
        content=PIXEL_GIF,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.get("/t/c/{token}/{link_index}")
def track_email_click(
    token: str,
    link_index: int,
    u: str = Query(..., description="Base64-encoded original URL"),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Click tracking redirect — records click then 302-redirects to original URL.
    The original URL is in the `u` query parameter (base64-encoded) so the
    redirect always works even if the DB is down.
    """
    # Decode original URL first — this MUST succeed for the redirect to work
    try:
        original_url = EmailTrackingService.decode_url(u)
    except Exception:
        # If decode fails, try treating as plain URL
        original_url = u

    # Record the click (best-effort — don't break the redirect on DB errors)
    try:
        user_agent = request.headers.get("user-agent", "") if request else ""
        svc = EmailTrackingService()
        svc.record_click(db, token, link_index, original_url, user_agent)
    except Exception as e:
        logger.warning("Click recording failed (token=%s, link=%d): %s", token, link_index, e)

    return RedirectResponse(url=original_url, status_code=302)
