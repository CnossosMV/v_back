"""
Web Push Notification Service

Sends push notifications to user's registered browsers/devices.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session

from app.models import PushSubscription, ProjectMember
from app.services.authorization_service import ROLE_HIERARCHY

logger = logging.getLogger(__name__)

PERMANENT_WEBPUSH_FAILURE_CODES = {404, 410}


def _get_vapid_claims() -> dict:
    return {
        "sub": os.getenv("VAPID_SUBJECT", "mailto:support@versya.io"),
    }


def _get_vapid_private_key() -> Optional[str]:
    return os.getenv("VAPID_PRIVATE_KEY")


def get_vapid_public_key() -> Optional[str]:
    return os.getenv("VAPID_PUBLIC_KEY")


def _webpush_failure_status_code(exc: Exception) -> Optional[int]:
    """Best-effort status extraction across pywebpush exception shapes."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(response, "status", None)

    if status_code is not None:
        try:
            return int(status_code)
        except (TypeError, ValueError):
            pass

    message = str(exc).lower()
    if "410" in message or "gone" in message:
        return 410
    if "404" in message or "not found" in message:
        return 404
    return None


def _is_permanent_webpush_failure(exc: Exception) -> bool:
    return _webpush_failure_status_code(exc) in PERMANENT_WEBPUSH_FAILURE_CODES


class PushNotificationService:
    def __init__(self, db: Session):
        self.db = db

    def subscribe(
        self,
        user_id: int,
        endpoint: str,
        p256dh_key: str,
        auth_key: str,
        device_label: Optional[str] = None,
    ) -> PushSubscription:
        """Register or re-activate a push subscription."""
        existing = (
            self.db.query(PushSubscription)
            .filter(PushSubscription.endpoint == endpoint)
            .first()
        )

        if existing:
            existing.user_id = user_id
            existing.p256dh_key = p256dh_key
            existing.auth_key = auth_key
            existing.is_active = True
            existing.device_label = device_label or existing.device_label
            existing.last_used_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(existing)
            return existing

        sub = PushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            p256dh_key=p256dh_key,
            auth_key=auth_key,
            device_label=device_label,
        )
        self.db.add(sub)
        self.db.commit()
        self.db.refresh(sub)
        return sub

    def unsubscribe(self, endpoint: str) -> bool:
        """Deactivate a push subscription by endpoint."""
        sub = (
            self.db.query(PushSubscription)
            .filter(PushSubscription.endpoint == endpoint)
            .first()
        )
        if not sub:
            return False
        sub.is_active = False
        self.db.commit()
        return True

    def get_user_subscriptions(self, user_id: int) -> List[PushSubscription]:
        """Get all active subscriptions for a user."""
        return (
            self.db.query(PushSubscription)
            .filter(
                PushSubscription.user_id == user_id,
                PushSubscription.is_active == True,
            )
            .all()
        )

    def send_notification(
        self,
        user_id: int,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Send push notification to all active subscriptions for a user. Returns count sent."""
        subs = self.get_user_subscriptions(user_id)
        return self._send_to_subs(subs, title, body, data)

    def send_to_project_agents(
        self,
        project_id: int,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Send push to all support_agent+ members of a project."""
        min_level = ROLE_HIERARCHY["support_agent"]
        members = (
            self.db.query(ProjectMember)
            .filter(
                ProjectMember.project_id == project_id,
                ProjectMember.is_active == True,
            )
            .all()
        )
        agent_ids = [
            m.user_id
            for m in members
            if ROLE_HIERARCHY.get(m.role, 0) >= min_level
        ]

        if not agent_ids:
            logger.warning(f"[PUSH] No agents with support_agent+ role for project {project_id} "
                           f"(total members: {len(members)}, roles: {[m.role for m in members]})")
            return 0

        subs = (
            self.db.query(PushSubscription)
            .filter(
                PushSubscription.user_id.in_(agent_ids),
                PushSubscription.is_active == True,
            )
            .all()
        )

        if not subs:
            logger.warning(f"[PUSH] No active push subscriptions for agents {agent_ids} in project {project_id}")
            return 0

        logger.info(f"[PUSH] Sending '{title}' to {len(subs)} subscription(s) for project {project_id}")
        return self._send_to_subs(subs, title, body, data)

    def _send_to_subs(
        self,
        subs: List[PushSubscription],
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Send push notification to a list of subscriptions."""
        private_key = _get_vapid_private_key()
        if not private_key:
            logger.warning("VAPID_PRIVATE_KEY not set — push notifications disabled")
            return 0

        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            logger.warning("pywebpush not installed — push notifications disabled")
            return 0

        vapid_claims = _get_vapid_claims()
        payload = json.dumps({
            "title": title,
            "body": body,
            "data": data or {},
        })

        sent = 0
        for sub in subs:
            subscription_info = {
                "endpoint": sub.endpoint,
                "keys": {
                    "p256dh": sub.p256dh_key,
                    "auth": sub.auth_key,
                },
            }
            try:
                webpush(
                    subscription_info=subscription_info,
                    data=payload,
                    vapid_private_key=private_key,
                    vapid_claims=vapid_claims,
                )
                sub.last_used_at = datetime.utcnow()
                sent += 1
            except WebPushException as e:
                status_code = _webpush_failure_status_code(e)
                if _is_permanent_webpush_failure(e):
                    sub.is_active = False
                    logger.info(
                        f"Push subscription {sub.id} permanently failed "
                        f"({status_code or 'unknown'}), deactivated"
                    )
                else:
                    logger.error(f"Push to sub {sub.id} failed: {e}")
            except Exception as e:
                logger.error(f"Push to sub {sub.id} error: {e}")

        self.db.commit()
        return sent


def get_push_service(db: Session) -> PushNotificationService:
    return PushNotificationService(db)
