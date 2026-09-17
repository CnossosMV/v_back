"""
Webhook Processor Service

Takes a queued WebhookIngest record, transforms the payload into a normalized
event, resolves the contact, creates a MessagingEvent, and runs it through
the standard event pipeline (schema discovery, event actions, scoring, funnels).
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import WebhookSource, WebhookIngest
from app.models.messaging import MessagingEvent
from .transformers import get_transformer, NormalizedEvent

logger = logging.getLogger(__name__)

# Retry schedule: 1s, 10s, 60s
RETRY_DELAYS = [1, 10, 60]
MAX_RETRIES = len(RETRY_DELAYS)


class WebhookProcessor:
    """Processes individual webhook ingest records."""

    def __init__(self, db: Session):
        self.db = db

    def process_ingest(self, ingest: WebhookIngest) -> bool:
        """
        Process a single ingest record.

        Returns True if processing succeeded, False if it failed
        (and should be retried if retries remain).
        """
        ingest.processing_status = "processing"
        ingest.updated_at = datetime.utcnow()
        self.db.commit()

        try:
            # Load source
            source = self.db.query(WebhookSource).filter(
                WebhookSource.id == ingest.source_id
            ).first()
            if not source:
                ingest.processing_status = "failed"
                ingest.error_message = "Source not found"
                self.db.commit()
                return False

            # Get transformer
            transformer = get_transformer(
                source.source_type, source.source_slug, source.transformer_config
            )

            # Transform
            normalized = transformer.transform(ingest.raw_payload or {}, ingest.id)
            if not normalized:
                ingest.processing_status = "failed"
                ingest.error_message = "Transformer returned None"
                self.db.commit()
                return False

            # Check contact reference — at least one identifier required
            ref = normalized.contact_ref
            if not any([ref.get("external_id"), ref.get("email"), ref.get("phone")]):
                ingest.processing_status = "failed"
                ingest.error_message = "No contact identifier (external_id, email, or phone) in payload"
                self.db.commit()
                return False

            # Resolve contact via identity resolver
            from app.services.messaging.identity_resolver import IdentityResolver
            resolver = IdentityResolver()
            user = resolver.resolve_identity(
                db=self.db,
                project_id=ingest.project_id,
                external_id=ref.get("external_id"),
                email=ref.get("email"),
                phone=ref.get("phone"),
                created_via=f"webhook:{ingest.source_slug}",
            )

            # Create MessagingEvent
            webhook_source_label = f"webhook:{ingest.source_slug}"
            event = MessagingEvent(
                project_id=ingest.project_id,
                user_id=user.id if user else None,
                event_name=normalized.event_name,
                properties=normalized.properties or {},
                source=webhook_source_label,
                created_at=normalized.occurred_at or datetime.utcnow(),
            )
            self.db.add(event)
            self.db.flush()

            # Link event to ingest
            ingest.resulting_event_id = event.id

            # Schema discovery
            try:
                from app.services.messaging.schema_discovery import schema_discovery
                schema_discovery.discover_from_event(
                    self.db, ingest.project_id, normalized.event_name, normalized.properties
                )
            except Exception as e:
                logger.warning("Schema discovery failed for ingest %d: %s", ingest.id, e)

            # Process through event pipeline
            try:
                import asyncio
                from app.services.messaging.event_processor import EventProcessor
                processor = EventProcessor()

                # Run async processor in sync context
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(processor.process_event(self.db, event))
                else:
                    loop.run_until_complete(processor.process_event(self.db, event))
            except Exception as e:
                logger.warning("Event pipeline processing failed for ingest %d: %s", ingest.id, e)
                # Don't fail the ingest — event was created successfully

            # Update counters on source
            source.last_received_at = ingest.received_at
            source.total_received = (source.total_received or 0) + 1

            ingest.processing_status = "completed"
            ingest.updated_at = datetime.utcnow()
            self.db.commit()
            return True

        except Exception as e:
            self.db.rollback()
            logger.error("Error processing ingest %d: %s", ingest.id, e, exc_info=True)

            # Schedule retry if attempts remain
            retry_count = (ingest.retry_count or 0)
            if retry_count < MAX_RETRIES:
                from datetime import timedelta
                delay = RETRY_DELAYS[retry_count]
                ingest.retry_count = retry_count + 1
                ingest.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
                ingest.processing_status = "queued"
                ingest.error_message = str(e)[:1000]
            else:
                ingest.processing_status = "failed"
                ingest.error_message = str(e)[:1000]
                # Update failure counter on source
                try:
                    source = self.db.query(WebhookSource).filter(
                        WebhookSource.id == ingest.source_id
                    ).first()
                    if source:
                        source.total_failed = (source.total_failed or 0) + 1
                except Exception:
                    pass

            ingest.updated_at = datetime.utcnow()
            self.db.commit()
            return False
