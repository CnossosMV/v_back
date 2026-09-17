"""
Push Notifications Router

Manages Web Push subscriptions and VAPID key distribution.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.routers.auth import get_current_user
from app.services.push_notification_service import get_push_service, get_vapid_public_key

router = APIRouter(tags=["push-notifications"])


class SubscribeRequest(BaseModel):
    endpoint: str
    p256dh_key: str
    auth_key: str
    device_label: Optional[str] = None


class UnsubscribeRequest(BaseModel):
    endpoint: str


class SubscriptionResponse(BaseModel):
    id: int
    endpoint: str
    device_label: Optional[str]
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime]

    class Config:
        from_attributes = True


@router.get("/push/vapid-key")
async def get_vapid_key():
    """Get the VAPID public key for push subscription."""
    key = get_vapid_public_key()
    if not key:
        raise HTTPException(status_code=503, detail="Push notifications not configured")
    return {"public_key": key}


@router.post("/push/subscribe", response_model=SubscriptionResponse)
async def subscribe(
    request: SubscribeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Register a push subscription for the current user."""
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    service = get_push_service(db)
    sub = service.subscribe(
        user_id=user_id,
        endpoint=request.endpoint,
        p256dh_key=request.p256dh_key,
        auth_key=request.auth_key,
        device_label=request.device_label,
    )
    return SubscriptionResponse.model_validate(sub)


@router.delete("/push/unsubscribe")
async def unsubscribe(
    request: UnsubscribeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Unsubscribe a push endpoint."""
    service = get_push_service(db)
    success = service.unsubscribe(request.endpoint)
    if not success:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"detail": "Unsubscribed"}


@router.post("/push/test")
async def send_test_notification(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Send a test push notification to the current user's devices."""
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    service = get_push_service(db)
    sent = service.send_notification(
        user_id=user_id,
        title="Versya Support",
        body="This is a test notification",
        data={"url": "/support"},
    )
    if sent == 0:
        raise HTTPException(status_code=404, detail="No active subscriptions found")
    return {"detail": f"Test notification sent to {sent} device(s)"}


@router.get("/push/subscriptions", response_model=List[SubscriptionResponse])
async def list_subscriptions(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List active push subscriptions for the current user."""
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    service = get_push_service(db)
    subs = service.get_user_subscriptions(user_id)
    return [SubscriptionResponse.model_validate(s) for s in subs]
