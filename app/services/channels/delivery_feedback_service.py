"""
Channel-agnostic delivery feedback service.

Processes bounces, complaints, unreachable contacts from all channels.
Takes auto-actions (opt-out, flag) and emits events for the automation pipeline.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DeliveryFeedback, ProjectSendConfig
from app.models.messaging import MessagingEvent, MessagingUser

logger = logging.getLogger(__name__)

# Error code → feedback_type classification
EMAIL_ERROR_CODES = {
    # Hard bounces (5xx permanent)
    "550": "hard_bounce", "551": "hard_bounce", "552": "hard_bounce",
    "553": "hard_bounce", "554": "hard_bounce",  # can be temp or perm
    # Soft bounces (4xx temporary)
    "421": "soft_bounce", "450": "soft_bounce", "451": "soft_bounce",
    "452": "soft_bounce",
}

# WhatsApp Meta error codes
WHATSAPP_ERROR_CODES = {
    # Permanent — number genuinely can't receive WhatsApp
    "131047": "unreachable",              # Number not on WhatsApp → opt out
    # Delivery failures — temporary/ambiguous, NOT consent signals
    "131026": "temporarily_unreachable",  # Message undeliverable (phone off, no signal, etc.)
    "131051": "temporarily_unreachable",  # Recipient not reachable (temporary)
    # Permanent blocks — WhatsApp/user blocked the business
    "131053": "blocked",                  # Phone number has been blocked
    # Policy — sender-side issue, not contact issue
    "470": "policy_violation",            # Policy/rate limit violation
}

# Evolution API error codes (HTTP status based)
EVOLUTION_ERROR_CODES = {
    "400": "temporarily_unreachable",     # Bad request / temp issue
    "404": "unreachable",                 # Number not found
    "429": "policy_violation",            # Rate limited
    "500": "temporarily_unreachable",     # Server error
}

# Twilio error codes
TWILIO_ERROR_CODES = {
    "30005": "unreachable",    # Unknown destination handset
    "30006": "unreachable",    # Landline or unreachable carrier
    "30003": "unreachable",    # Unreachable destination
    "21610": "blocked",        # Number unsubscribed (STOP)
    "21612": "blocked",        # Number not permitted
    "21614": "unreachable",    # Not a valid mobile number
}

# feedback_type → auto-action mapping
AUTO_ACTIONS = {
    "hard_bounce": "opted_out",
    "unreachable": "opted_out",
    "blocked": "opted_out",
    "complaint": "global_opted_out",
    "soft_bounce": "check_threshold",
    "temporarily_unreachable": "check_threshold",
    "expired": "logged",
    "policy_violation": "logged",
}


class DeliveryFeedbackService:
    def __init__(self, db: Session):
        self.db = db

    def record_feedback(
        self,
        project_id: int,
        channel: str,
        recipient: str,
        feedback_type: str,
        reason: Optional[str] = None,
        provider: Optional[str] = None,
        provider_code: Optional[str] = None,
        provider_detail: Optional[str] = None,
        send_log_id: Optional[int] = None,
        raw_payload: Optional[dict] = None,
        user_id: Optional[int] = None,
    ) -> Optional[DeliveryFeedback]:
        """Record delivery feedback and take auto-action."""
        # Resolve user if not provided
        if not user_id:
            user_id = self._resolve_user_id(project_id, channel, recipient)

        # Attribute to a sending domain for reputation. Provider webhooks often
        # lack a send_log_id, so fall back to the most recent send to this
        # recipient that carries a resolved domain.
        sending_domain = self._resolve_sending_domain(project_id, channel, recipient, send_log_id)

        feedback = DeliveryFeedback(
            project_id=project_id,
            user_id=user_id,
            channel=channel,
            recipient=recipient[:255] if recipient else "",
            feedback_type=feedback_type,
            reason=reason[:500] if reason else None,
            provider=provider,
            provider_code=provider_code,
            provider_detail=provider_detail,
            raw_payload=raw_payload,
            send_log_id=send_log_id,
            sending_domain=sending_domain,
        )

        try:
            action = self._auto_action(feedback, project_id)
            feedback.action_taken = action
            self.db.add(feedback)
            self.db.flush()
            logger.info(
                "Delivery feedback recorded: project=%s channel=%s type=%s action=%s recipient=%s",
                project_id, channel, feedback_type, action, recipient[:30],
            )
        except Exception:
            logger.exception("Failed to record delivery feedback")
            return None

        # Negative feedback changes the message's effectiveness — recompute.
        # Complaints/opt-outs affect the user's other recent sends too.
        try:
            from app.services.scoring.mes_engine import MESEngine
            engine = MESEngine(self.db)
            if send_log_id:
                engine.mark_stale(send_log_id)
            if user_id:
                engine.mark_stale_for_user(project_id, user_id)
        except Exception:
            logger.warning("Failed to mark MES stale for delivery feedback", exc_info=True)

        return feedback

    def _auto_action(self, feedback: DeliveryFeedback, project_id: int) -> str:
        """Determine and execute action based on feedback type."""
        action_rule = AUTO_ACTIONS.get(feedback.feedback_type, "logged")

        if action_rule == "opted_out" and feedback.user_id:
            self._opt_out_channel(project_id, feedback.user_id, feedback.channel)
            # Mark whatsapp_status=invalid for permanent WhatsApp errors (e.g. 131047)
            if feedback.channel == "whatsapp" and feedback.feedback_type == "unreachable":
                self._mark_whatsapp_invalid(project_id, feedback.user_id)
            self._emit_event(project_id, feedback.user_id, "contact.channel_opted_out", {
                "channel": feedback.channel,
                "reason": feedback.reason,
                "feedback_type": feedback.feedback_type,
                "provider_code": feedback.provider_code,
            })
            # Emit channel-specific event too
            channel_event = {
                "email": "contact.email_bounced",
                "whatsapp": "contact.whatsapp_unreachable",
                "sms": "contact.sms_unreachable",
            }.get(feedback.channel)
            if channel_event:
                self._emit_event(project_id, feedback.user_id, channel_event, {
                    "bounce_type": feedback.feedback_type,
                    "reason": feedback.reason,
                    "recipient": feedback.recipient,
                    "provider_code": feedback.provider_code,
                })
            return "opted_out"

        if action_rule == "global_opted_out" and feedback.user_id:
            self._global_opt_out(project_id, feedback.user_id)
            self._emit_event(project_id, feedback.user_id, "contact.global_opted_out", {
                "reason": feedback.reason,
                "feedback_type": feedback.feedback_type,
                "provider": feedback.provider,
            })
            self._emit_event(project_id, feedback.user_id, "contact.complaint", {
                "channel": feedback.channel,
                "provider": feedback.provider,
            })
            return "global_opted_out"

        if action_rule == "check_threshold" and feedback.user_id:
            threshold, window = self._get_soft_bounce_config(project_id)
            since = datetime.utcnow() - timedelta(days=window)

            # Count feedback but EXCLUDE retries of the same original send.
            # Retries aren't independent signals — they're the same delivery attempt.
            from app.models import SendLog
            retry_log_ids = self.db.query(SendLog.id).filter(
                SendLog.source_type == "retry",
                SendLog.user_id == feedback.user_id,
                SendLog.project_id == project_id,
            ).subquery()

            count = self.db.query(func.count(DeliveryFeedback.id)).filter(
                DeliveryFeedback.project_id == project_id,
                DeliveryFeedback.user_id == feedback.user_id,
                DeliveryFeedback.channel == feedback.channel,
                DeliveryFeedback.feedback_type == feedback.feedback_type,
                DeliveryFeedback.created_at >= since,
                ~DeliveryFeedback.send_log_id.in_(retry_log_ids),
            ).scalar() or 0
            # +1 for the current one (not yet committed)
            if count + 1 >= threshold:
                self._opt_out_channel(project_id, feedback.user_id, feedback.channel)
                self._emit_event(project_id, feedback.user_id, "contact.channel_opted_out", {
                    "channel": feedback.channel,
                    "reason": f"Soft bounce threshold exceeded ({count + 1}/{threshold} in {window}d)",
                    "feedback_type": "soft_bounce_threshold",
                    "provider_code": feedback.provider_code,
                })
                channel_event = {"email": "contact.email_bounced", "whatsapp": "contact.whatsapp_unreachable"}.get(feedback.channel)
                if channel_event:
                    self._emit_event(project_id, feedback.user_id, channel_event, {
                        "bounce_type": f"{feedback.feedback_type}_threshold",
                        "reason": feedback.reason,
                        "recipient": feedback.recipient,
                        "count": count + 1,
                    })
                return "opted_out"
            # Below threshold — just log
            self._emit_event(project_id, feedback.user_id, f"contact.{feedback.channel}_bounced" if feedback.channel == "email" else f"contact.{feedback.channel}_failed", {
                "bounce_type": "soft_bounce",
                "reason": feedback.reason,
                "recipient": feedback.recipient,
                "provider_code": feedback.provider_code,
            })
            return "logged"

        return "logged"

    def _opt_out_channel(self, project_id: int, user_id: int, channel: str) -> None:
        """Add channel to user's opted_out_channels."""
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()
        if not user:
            return
        channels = list(user.opted_out_channels or [])
        if channel not in channels:
            channels.append(channel)
            user.opted_out_channels = channels
            logger.info("Opted out user %s from channel %s (project %s)", user_id, channel, project_id)

    def _mark_whatsapp_invalid(self, project_id: int, user_id: int) -> None:
        """Mark contact's WhatsApp status as invalid (number not on WhatsApp)."""
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()
        if user:
            user.whatsapp_status = "invalid"
            user.whatsapp_checked_at = datetime.utcnow()

    def _global_opt_out(self, project_id: int, user_id: int) -> None:
        """Set global_opt_out=True."""
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()
        if user:
            user.global_opt_out = True
            logger.info("Global opt-out for user %s (project %s)", user_id, project_id)

    def _emit_event(self, project_id: int, user_id: int, event_name: str, properties: dict) -> None:
        """Emit a MessagingEvent for the automation pipeline."""
        try:
            event = MessagingEvent(
                project_id=project_id,
                user_id=user_id,
                event_name=event_name,
                properties=properties,
                source="system",
                processed=False,
            )
            self.db.add(event)
        except Exception:
            logger.exception("Failed to emit event %s for user %s", event_name, user_id)

    def _resolve_sending_domain(
        self, project_id: int, channel: str, recipient: str, send_log_id: Optional[int],
    ) -> Optional[str]:
        """Best-effort sending-domain attribution for a feedback event."""
        from app.models import SendLog
        try:
            if send_log_id:
                sl = self.db.query(SendLog.sending_domain).filter(SendLog.id == send_log_id).first()
                if sl and sl[0]:
                    return sl[0]
            if recipient:
                sl = (
                    self.db.query(SendLog.sending_domain)
                    .filter(
                        SendLog.project_id == project_id,
                        SendLog.channel == channel,
                        SendLog.recipient == recipient,
                        SendLog.sending_domain.isnot(None),
                    )
                    .order_by(SendLog.id.desc())
                    .first()
                )
                if sl and sl[0]:
                    return sl[0]
        except Exception:
            logger.debug("sending-domain attribution failed", exc_info=True)
        return None

    def _resolve_user_id(self, project_id: int, channel: str, recipient: str) -> Optional[int]:
        """Resolve user_id from recipient address."""
        if not recipient:
            return None
        user = self.db.query(MessagingUser.id).filter(
            MessagingUser.project_id == project_id,
        ).filter(
            (MessagingUser.email == recipient)
            | (MessagingUser.phone == recipient)
            | (MessagingUser.phone_e164 == recipient)
            | (MessagingUser.external_id == recipient)
        ).first()
        return user.id if user else None

    def _get_soft_bounce_config(self, project_id: int) -> tuple[int, int]:
        """Get soft bounce threshold and window from project config."""
        config = self.db.query(ProjectSendConfig).filter(
            ProjectSendConfig.project_id == project_id,
        ).first()
        if config:
            return config.soft_bounce_threshold, config.soft_bounce_window_days
        return 3, 7  # defaults

    def get_feedback_for_send_log(self, send_log_id: int) -> list[DeliveryFeedback]:
        """Get all feedback records for a specific send log."""
        return self.db.query(DeliveryFeedback).filter(
            DeliveryFeedback.send_log_id == send_log_id,
        ).order_by(DeliveryFeedback.created_at.desc()).all()

    @staticmethod
    def classify_email_error(error_code: Optional[str], error_message: Optional[str] = None) -> str:
        """Classify an email error into a feedback_type."""
        if error_code and error_code[:3] in EMAIL_ERROR_CODES:
            return EMAIL_ERROR_CODES[error_code[:3]]
        if error_message:
            lower = error_message.lower()
            if any(w in lower for w in ("does not exist", "no such user", "unknown user", "invalid recipient", "not found")):
                return "hard_bounce"
            if any(w in lower for w in ("over quota", "storage", "full", "rate limit", "try again")):
                return "soft_bounce"
            if any(w in lower for w in ("spam", "abuse", "complaint", "blocked", "rejected")):
                return "complaint"
        return "soft_bounce"  # default to soft if unclear

    @staticmethod
    def classify_whatsapp_error(error_code: Optional[str], provider: Optional[str] = None) -> str:
        """Classify a WhatsApp error into a feedback_type.

        Checks Meta error codes first, then Evolution HTTP status codes.
        """
        code = str(error_code) if error_code else None
        if code:
            if code in WHATSAPP_ERROR_CODES:
                return WHATSAPP_ERROR_CODES[code]
            if provider == "evolution" and code in EVOLUTION_ERROR_CODES:
                return EVOLUTION_ERROR_CODES[code]
        return "temporarily_unreachable"  # default to temporary — don't permanently block

    @staticmethod
    def classify_sms_error(error_code: Optional[str]) -> str:
        """Classify an SMS/Twilio error into a feedback_type."""
        if error_code and str(error_code) in TWILIO_ERROR_CODES:
            return TWILIO_ERROR_CODES[str(error_code)]
        return "soft_bounce"

    @staticmethod
    def classify_error(channel: str, error_code: Optional[str], error_message: Optional[str] = None, provider: Optional[str] = None) -> str:
        """Classify any channel error into a feedback_type."""
        if channel == "email":
            return DeliveryFeedbackService.classify_email_error(error_code, error_message)
        if channel == "whatsapp":
            return DeliveryFeedbackService.classify_whatsapp_error(error_code, provider)
        if channel == "sms":
            return DeliveryFeedbackService.classify_sms_error(error_code)
        return "soft_bounce"
