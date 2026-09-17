"""Signed outgoing webhooks for audience sync state changes."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.messaging import (
    MessagingAudienceDestination,
    MessagingAudienceSyncJob,
    MessagingAudienceWebhookDelivery,
    MessagingAudienceWebhookEndpoint,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
DEFAULT_EVENTS = {
    "audience.sync.succeeded",
    "audience.sync.partial_failed",
    "audience.sync.failed",
    "audience.provider_status.updated",
}


def _safe_response_body(text: str) -> str:
    return text[:1000] if text else ""


def _body_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


class AudienceWebhookDeliveryService:
    def queue_sync_job_events(
        self,
        db: Session,
        job: MessagingAudienceSyncJob,
        audience_destination: Optional[MessagingAudienceDestination] = None,
    ) -> int:
        event_name = self._event_name_for_job(job)
        queued = self.queue_event(
            db,
            project_id=job.project_id,
            event_name=event_name,
            payload=self._payload_for_job(job, event_name, audience_destination),
            audience_id=job.audience_id,
            audience_destination_id=job.audience_destination_id,
            sync_job_id=job.id,
            commit=False,
        )
        queued += self.queue_event(
            db,
            project_id=job.project_id,
            event_name="audience.provider_status.updated",
            payload=self._payload_for_job(job, "audience.provider_status.updated", audience_destination),
            audience_id=job.audience_id,
            audience_destination_id=job.audience_destination_id,
            sync_job_id=job.id,
            commit=False,
        )
        db.commit()
        return queued

    def queue_event(
        self,
        db: Session,
        *,
        project_id: int,
        event_name: str,
        payload: Dict[str, Any],
        audience_id: Optional[int] = None,
        audience_destination_id: Optional[int] = None,
        sync_job_id: Optional[int] = None,
        commit: bool = True,
    ) -> int:
        endpoints = db.query(MessagingAudienceWebhookEndpoint).filter(
            MessagingAudienceWebhookEndpoint.project_id == project_id,
            MessagingAudienceWebhookEndpoint.is_active == True,
        ).all()
        queued = 0
        for endpoint in endpoints:
            enabled_events = set(endpoint.events or DEFAULT_EVENTS)
            if event_name not in enabled_events:
                continue
            db.add(MessagingAudienceWebhookDelivery(
                project_id=project_id,
                webhook_endpoint_id=endpoint.id,
                audience_id=audience_id,
                audience_destination_id=audience_destination_id,
                sync_job_id=sync_job_id,
                event_name=event_name,
                status="queued",
                payload_summary=payload,
            ))
            queued += 1
        if commit and queued:
            db.commit()
        return queued

    def deliver_due(self, db: Session, limit: int = 50) -> int:
        now = datetime.utcnow()
        rows = db.query(MessagingAudienceWebhookDelivery).filter(
            MessagingAudienceWebhookDelivery.status.in_(["queued", "retry"]),
            or_(
                MessagingAudienceWebhookDelivery.next_retry_at == None,
                MessagingAudienceWebhookDelivery.next_retry_at <= now,
            ),
        ).order_by(MessagingAudienceWebhookDelivery.created_at.asc()).limit(limit).all()
        delivered = 0
        for delivery in rows:
            endpoint = delivery.endpoint
            if not endpoint or not endpoint.is_active:
                delivery.status = "failed"
                delivery.error_message = "Webhook endpoint is inactive"
                continue
            self._deliver_one(db, delivery, endpoint)
            delivered += 1
        db.commit()
        return delivered

    def _deliver_one(
        self,
        db: Session,
        delivery: MessagingAudienceWebhookDelivery,
        endpoint: MessagingAudienceWebhookEndpoint,
    ) -> None:
        payload = dict(delivery.payload_summary or {})
        payload.setdefault("event", delivery.event_name)
        payload.setdefault("delivery_id", delivery.id)
        payload.setdefault("project_id", delivery.project_id)
        payload.setdefault("occurred_at", datetime.utcnow().isoformat())
        body = _body_bytes(payload)
        timestamp = str(int(time.time()))
        signature = hmac.new((endpoint.secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Versya-Event": delivery.event_name,
            "X-Versya-Timestamp": timestamp,
            "X-Versya-Signature": f"sha256={signature}",
        }
        delivery.attempt_count = int(delivery.attempt_count or 0) + 1
        try:
            with httpx.Client(timeout=15.0, follow_redirects=False) as client:
                response = client.post(endpoint.url, content=body, headers=headers)
            delivery.response_status = response.status_code
            delivery.response_body = _safe_response_body(response.text)
            if 200 <= response.status_code < 300:
                delivery.status = "sent"
                delivery.delivered_at = datetime.utcnow()
                delivery.error_message = None
                endpoint.last_delivery_status = "sent"
                endpoint.last_delivered_at = delivery.delivered_at
                endpoint.last_error = None
                return
            self._mark_retry_or_failed(delivery, endpoint, f"HTTP {response.status_code}")
        except Exception as exc:
            logger.warning("Audience webhook delivery failed endpoint=%s delivery=%s: %s", endpoint.id, delivery.id, exc)
            self._mark_retry_or_failed(delivery, endpoint, str(exc))

    def _mark_retry_or_failed(
        self,
        delivery: MessagingAudienceWebhookDelivery,
        endpoint: MessagingAudienceWebhookEndpoint,
        error: str,
    ) -> None:
        delivery.error_message = error
        endpoint.last_error = error
        if delivery.attempt_count >= MAX_ATTEMPTS:
            delivery.status = "failed"
            endpoint.last_delivery_status = "failed"
            return
        delay_seconds = min(3600, 30 * (2 ** max(0, delivery.attempt_count - 1)))
        delivery.status = "retry"
        delivery.next_retry_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
        endpoint.last_delivery_status = "retry"

    def _event_name_for_job(self, job: MessagingAudienceSyncJob) -> str:
        if job.status in {"succeeded", "dry_run"}:
            return "audience.sync.succeeded"
        if job.status in {"partial_failed", "skipped"}:
            return "audience.sync.partial_failed"
        return "audience.sync.failed"

    def _payload_for_job(
        self,
        job: MessagingAudienceSyncJob,
        event_name: str,
        audience_destination: Optional[MessagingAudienceDestination],
    ) -> Dict[str, Any]:
        return {
            "event": event_name,
            "project_id": job.project_id,
            "audience_id": job.audience_id,
            "audience_destination_id": job.audience_destination_id,
            "sync_job_id": job.id,
            "provider_type": job.provider_type,
            "status": job.status,
            "dry_run": bool(job.dry_run),
            "external_audience_id": audience_destination.external_audience_id if audience_destination else None,
            "counts": {
                "total": job.total_count,
                "eligible": job.eligible_count,
                "add": job.add_count,
                "remove": job.remove_count,
                "failed": job.failed_count,
            },
            "request_summary": job.request_summary,
            "response_summary": job.response_summary,
            "error_message": job.error_message,
            "occurred_at": (job.finished_at or job.created_at or datetime.utcnow()).isoformat(),
        }


audience_webhook_delivery_service = AudienceWebhookDeliveryService()
