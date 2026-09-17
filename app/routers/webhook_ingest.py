"""
Webhook Ingest Router (Public)

Receives raw webhook payloads from third-party services.
No JWT required — authenticated via webhook signing secret.

Endpoint: POST /api/v1/projects/{project_id}/webhooks/{source_slug}
"""
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from threading import Lock

from fastapi import APIRouter, Request, HTTPException

from app.database import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/webhooks",
    tags=["Webhook Ingest"],
)


# ── Simple in-memory rate limiter ──────────────────────────────────────

class _RateLimiter:
    """Per-key sliding window rate limiter."""

    def __init__(self):
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def is_limited(self, key: str, max_per_minute: int) -> bool:
        now = time.time()
        cutoff = now - 60.0
        with self._lock:
            window = self._windows[key]
            # Prune old entries
            self._windows[key] = [t for t in window if t > cutoff]
            if len(self._windows[key]) >= max_per_minute:
                return True
            self._windows[key].append(now)
            return False


_rate_limiter = _RateLimiter()

DEFAULT_RATE_LIMIT = 100  # per minute per source
PROJECT_RATE_LIMIT = 500  # per minute per project (all sources)
DEDUP_WINDOW_HOURS = 72


# ── Public ingest endpoint ─────────────────────────────────────────────

@router.post("/{source_slug}")
async def receive_webhook(
    project_id: int,
    source_slug: str,
    request: Request,
):
    """
    Receive a webhook payload from a third-party service.

    1. Lookup source by (project_id, source_slug)
    2. Rate limit check (source-level + project-level)
    3. Read raw body
    4. Extract idempotency key, check dedup
    5. Verify signature
    6. Store WebhookIngest(status=queued)
    7. Return 200
    """
    db = SessionLocal()
    try:
        from app.models import WebhookSource, WebhookIngest

        # 1. Lookup source
        source = db.query(WebhookSource).filter(
            WebhookSource.project_id == project_id,
            WebhookSource.source_slug == source_slug,
        ).first()

        if not source:
            raise HTTPException(status_code=404, detail="Webhook source not found")

        if source.status != "active":
            raise HTTPException(status_code=404, detail="Webhook source is not active")

        # 2. Rate limit
        source_limit = source.rate_limit_per_minute or DEFAULT_RATE_LIMIT
        source_key = f"webhook:{project_id}:{source_slug}"
        project_key = f"webhook:{project_id}:*"

        if _rate_limiter.is_limited(source_key, source_limit):
            raise HTTPException(status_code=429, detail="Rate limit exceeded for this source")

        if _rate_limiter.is_limited(project_key, PROJECT_RATE_LIMIT):
            raise HTTPException(status_code=429, detail="Rate limit exceeded for this project")

        # 3. Read raw body
        raw_body = await request.body()

        # Parse JSON
        import json
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        # Extract headers as dict
        headers_dict = dict(request.headers)

        # 4. Idempotency key + dedup
        from app.services.webhook.transformers import get_transformer
        transformer = get_transformer(source.source_type, source.source_slug, source.transformer_config)
        idempotency_key = transformer.get_idempotency_key(payload)

        if idempotency_key:
            cutoff = datetime.utcnow() - timedelta(hours=DEDUP_WINDOW_HOURS)
            existing = db.query(WebhookIngest).filter(
                WebhookIngest.project_id == project_id,
                WebhookIngest.source_slug == source_slug,
                WebhookIngest.idempotency_key == idempotency_key,
                WebhookIngest.received_at >= cutoff,
            ).first()

            if existing:
                # Store as skipped for audit trail
                ingest = WebhookIngest(
                    project_id=project_id,
                    source_id=source.id,
                    source_slug=source_slug,
                    received_at=datetime.utcnow(),
                    headers=headers_dict,
                    raw_payload=payload,
                    signature_valid=None,
                    idempotency_key=idempotency_key,
                    processing_status="skipped",
                    error_message=f"Duplicate of ingest #{existing.id}",
                )
                db.add(ingest)
                db.commit()
                return {"status": "skipped", "reason": "duplicate", "ingest_id": ingest.id}

        # 5. Verify signature
        from app.services.webhook.signature_verifier import verify
        from app.services.encryption_service import decrypt_value

        secret = ""
        if source.secret_enc:
            try:
                secret = decrypt_value(source.secret_enc)
            except Exception:
                logger.warning("Failed to decrypt secret for source %d", source.id)

        sig_valid = verify(
            source_type=source.source_type,
            source_slug=source.source_slug,
            headers=headers_dict,
            raw_body=raw_body,
            secret=secret,
        )

        if not sig_valid:
            # Store failed ingest for debugging
            ingest = WebhookIngest(
                project_id=project_id,
                source_id=source.id,
                source_slug=source_slug,
                received_at=datetime.utcnow(),
                headers=headers_dict,
                raw_payload=payload,
                signature_valid=False,
                idempotency_key=idempotency_key,
                processing_status="failed",
                error_message="Invalid signature",
            )
            db.add(ingest)

            source.total_failed = (source.total_failed or 0) + 1
            db.commit()

            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        # 6. Store as queued
        ingest = WebhookIngest(
            project_id=project_id,
            source_id=source.id,
            source_slug=source_slug,
            received_at=datetime.utcnow(),
            headers=headers_dict,
            raw_payload=payload,
            signature_valid=True,
            idempotency_key=idempotency_key,
            processing_status="queued",
        )
        db.add(ingest)
        db.commit()
        db.refresh(ingest)

        return {"status": "accepted", "ingest_id": ingest.id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Webhook ingest error for %s/%s: %s", project_id, source_slug, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        db.close()
