"""
Delivery Tracker — unified delivery status handler for all channels.

Records DeliveryStatusEvent rows and advances SendLog.status through
the lifecycle: queued → sent → delivered → read (failed is terminal).

Also emits `channel.<channel>.<status>` MessagingEvents so the
automation pipeline (funnels, event actions, scoring) can react to
delivery lifecycle transitions.
"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import SendLog, DeliveryStatusEvent
from app.models.messaging import MessagingEvent

logger = logging.getLogger(__name__)

# Status progression order (higher = more advanced). Failed is terminal.
_STATUS_ORDER = {
    "queued": 0,
    "sent": 1,
    "delivered": 2,
    "read": 3,
}

# Map internal statuses to channel event suffixes
_STATUS_EVENT_MAP = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


class DeliveryTracker:
    def __init__(self, db: Session):
        self.db = db

    def record_status(
        self,
        provider_message_id: str,
        status: str,
        provider_status: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        raw_payload: Optional[Dict[str, Any]] = None,
        provider_timestamp: Optional[datetime] = None,
    ) -> Optional[int]:
        """
        Record a delivery status event and update the parent SendLog.

        Returns the DeliveryStatusEvent.id if a matching SendLog was found,
        None otherwise.
        """
        if not provider_message_id:
            return None

        # ORDER BY id DESC so duplicate webhook deliveries (e.g. Meta retries
        # its delivery callbacks) always match the same — most recent — row
        # for a given provider_message_id. This keeps the retry scheduler's
        # dedupe deterministic when deferred/retry flows produce sibling
        # SendLogs with the same wamid.
        send_log = self.db.query(SendLog).filter(
            SendLog.provider_message_id == provider_message_id,
        ).order_by(SendLog.id.desc()).with_for_update().first()

        if not send_log:
            # Try without angle brackets (Message-ID headers include them)
            clean_id = provider_message_id.strip("<>")
            if clean_id != provider_message_id:
                send_log = self.db.query(SendLog).filter(
                    SendLog.provider_message_id == clean_id,
                ).order_by(SendLog.id.desc()).with_for_update().first()

        if not send_log:
            # No matching send log — this message was sent before the send layer
            # was active, or via a path that doesn't create send logs.
            return None

        # Capture previous status before mutation
        previous_status = send_log.status

        # Create status event
        event = DeliveryStatusEvent(
            send_log_id=send_log.id,
            status=status,
            provider_status=provider_status,
            provider_timestamp=provider_timestamp,
            error_code=error_code,
            error_message=error_message,
            raw_payload=raw_payload,
        )
        self.db.add(event)

        # Update SendLog status (only advance, never go backward)
        now = datetime.utcnow()
        status_changed = False

        if status == "failed":
            if send_log.status == "failed":
                logger.info(
                    "DeliveryTracker: duplicate failed webhook ignored for side effects "
                    "send_log=%s provider_msg=%s",
                    send_log.id,
                    provider_message_id,
                )
            elif send_log.status in ("delivered", "read"):
                logger.warning(
                    "DeliveryTracker: ignoring failed webhook after terminal success "
                    "send_log=%s current_status=%s provider_msg=%s",
                    send_log.id,
                    send_log.status,
                    provider_message_id,
                )
            else:
                send_log.status = "failed"
                send_log.failed_at = now
                send_log.error_message = error_message or send_log.error_message
                status_changed = True
        else:
            current_order = _STATUS_ORDER.get(send_log.status, -1)
            new_order = _STATUS_ORDER.get(status, -1)

            if new_order > current_order:
                send_log.status = status
                status_changed = True

                if status == "sent" and not send_log.sent_at:
                    send_log.sent_at = provider_timestamp or now
                elif status == "delivered" and not send_log.delivered_at:
                    send_log.delivered_at = provider_timestamp or now
                elif status == "read" and not send_log.read_at:
                    send_log.read_at = provider_timestamp or now

        self.db.flush()

        # Emit channel event for the automation pipeline
        if status_changed and status in _STATUS_EVENT_MAP:
            self._emit_channel_event(
                send_log, status, previous_status,
                error_code=error_code, error_message=error_message,
            )

        # On failure, record delivery feedback for auto-actions (opt-out, etc.)
        if status == "failed" and status_changed:
            self._record_failure_feedback(send_log, error_code, error_message, raw_payload)
            # Transient WhatsApp errors still schedule a retry; 131026/130472 were
            # reclassified permanent (whatsapp_retry_policy) so they no longer do.
            self._maybe_schedule_whatsapp_retry(send_log, error_code, error_message)
            # Ambiguous "undeliverable" codes we no longer retry are a strong
            # signal the number may not be on WhatsApp. Re-run validation (uses
            # the project's Evolution instance when one is active) so the contact's
            # whatsapp_status gets refreshed to valid/invalid and future sends are
            # gated by _filter_opt_out instead of re-hitting Meta with a dead number.
            self._maybe_revalidate_whatsapp(send_log, error_code)

        # Mark MES record as stale for recomputation
        if status_changed:
            try:
                from app.services.scoring.mes_engine import MESEngine
                MESEngine(self.db).mark_stale(send_log.id)
            except Exception:
                pass

        logger.info(
            f"DeliveryTracker: send_log={send_log.id} status={status} "
            f"provider_msg={provider_message_id}"
        )
        return event.id

    def _maybe_schedule_whatsapp_retry(
        self,
        send_log: SendLog,
        error_code: Optional[str],
        error_message: Optional[str],
    ) -> None:
        """Schedule a retry for transient WhatsApp errors (e.g. Meta 131026).

        Best-effort: failure to schedule must not break the webhook handler.
        """
        try:
            from app.services.channels.whatsapp_retry_scheduler import (
                WhatsAppRetryScheduler,
            )
            WhatsAppRetryScheduler(self.db).maybe_schedule_retry(
                failed_log=send_log,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            logger.exception(
                "Failed to evaluate WhatsApp retry for send_log %s", send_log.id,
            )

    # Undeliverable codes that are ambiguous (number may or may not be on
    # WhatsApp). We no longer retry these; instead re-validate the number.
    _REVALIDATE_WA_CODES = frozenset({"131026", "131051"})

    def _maybe_revalidate_whatsapp(
        self,
        send_log: SendLog,
        error_code: Optional[str],
    ) -> None:
        """On an ambiguous WhatsApp delivery failure, force a fresh number
        validation (Evolution-backed when an active instance exists).

        Best-effort: must never break the webhook handler.
        """
        if (send_log.channel != "whatsapp"
                or not send_log.user_id
                or str(error_code or "") not in self._REVALIDATE_WA_CODES):
            return
        try:
            from app.models.messaging import MessagingUser
            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == send_log.user_id,
            ).first()
            if not user or not user.phone_e164:
                return

            # Force a recheck past the 7-day cache: a real delivery failure is a
            # stronger signal than the last ingestion-time check. Setting
            # 'checking' makes WhatsAppValidationService._should_skip return False.
            user.whatsapp_status = "checking"
            self.db.commit()

            import asyncio
            from app.services.whatsapp_validation_service import WhatsAppValidationService
            try:
                asyncio.get_running_loop()
                asyncio.create_task(
                    WhatsAppValidationService().validate_number(
                        send_log.project_id, user.id, user.phone_e164,
                    )
                )
            except RuntimeError:
                # No running loop (e.g. sync worker context) — run to completion.
                asyncio.run(
                    WhatsAppValidationService().validate_number(
                        send_log.project_id, user.id, user.phone_e164,
                    )
                )
        except Exception:
            logger.exception(
                "Failed to revalidate WhatsApp number for send_log %s", send_log.id,
            )

    def _record_failure_feedback(
        self,
        send_log: SendLog,
        error_code: Optional[str],
        error_message: Optional[str],
        raw_payload: Optional[Dict[str, Any]],
    ) -> None:
        """On delivery failure, classify the error and record feedback for auto-action."""
        try:
            from app.services.channels.delivery_feedback_service import DeliveryFeedbackService

            svc = DeliveryFeedbackService(self.db)

            # Determine provider from channel/instance (needed for error classification)
            provider = "unknown"
            if send_log.channel == "email":
                provider = "email"
            elif send_log.channel == "whatsapp":
                # Resolve Evolution vs Meta from instance
                provider = "whatsapp"
                if send_log.instance_id:
                    try:
                        from app.models import WhatsAppInstance
                        inst = self.db.query(WhatsAppInstance).filter(
                            WhatsAppInstance.id == send_log.instance_id,
                        ).first()
                        if inst and inst.provider_type == "evolution_api":
                            provider = "evolution"
                    except Exception:
                        pass
            elif send_log.channel == "sms":
                provider = "twilio"

            feedback_type = svc.classify_error(
                send_log.channel or "unknown", error_code, error_message, provider,
            )

            svc.record_feedback(
                project_id=send_log.project_id,
                channel=send_log.channel or "unknown",
                recipient=send_log.recipient,
                feedback_type=feedback_type,
                reason=error_message,
                provider=provider,
                provider_code=error_code,
                provider_detail=error_message,
                send_log_id=send_log.id,
                raw_payload=raw_payload,
                user_id=send_log.user_id,
            )
        except Exception:
            logger.exception("Failed to record delivery feedback for send_log %s", send_log.id)

    def _emit_channel_event(
        self,
        send_log: SendLog,
        status: str,
        previous_status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Emit a channel.<channel>.<status> MessagingEvent."""
        channel = send_log.channel or "unknown"
        event_suffix = _STATUS_EVENT_MAP.get(status, status)
        event_name = f"channel.{channel}.{event_suffix}"

        properties: Dict[str, Any] = {
            "send_log_id": send_log.id,
            "channel": channel,
            "recipient": send_log.recipient,
            "template_id": send_log.template_id,
            "source_type": send_log.source_type,
            "source_id": send_log.source_id,
            "provider_message_id": send_log.provider_message_id,
            "previous_status": previous_status,
            "instance_id": send_log.instance_id,
        }
        if error_code:
            properties["error_code"] = error_code
        if error_message:
            properties["error_message"] = error_message

        event = MessagingEvent(
            project_id=send_log.project_id,
            user_id=send_log.user_id,
            event_name=event_name,
            source="delivery_tracker",
            properties=properties,
        )
        self.db.add(event)
        self.db.flush()

        logger.debug(
            "Channel event emitted: %s for send_log=%d",
            event_name, send_log.id,
        )
