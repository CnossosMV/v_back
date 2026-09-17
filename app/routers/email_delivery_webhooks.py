"""
Email Delivery Feedback Webhooks — SES, Mailgun, SendGrid.

Receives bounce, complaint, delivery notifications and routes them
to DeliveryFeedbackService for auto-actions.
"""
import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SendLog, EmailInstance, CustomerSMTPConfig

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/webhooks/email",
    tags=["email-delivery-webhooks"],
)


async def _process_feedback(db: Session, source, body, source_label: str) -> dict:
    """Shared feedback processing for both EmailInstance and CustomerSMTPConfig.

    `source` must expose `.project_id` and `.id`.
    """
    # SES SNS: check for SubscriptionConfirmation first
    if isinstance(body, dict) and body.get("Type") == "SubscriptionConfirmation":
        return await _handle_sns_subscription_confirmation(body)

    # SES SNS Notification
    if isinstance(body, dict) and body.get("Type") == "Notification":
        return _handle_ses_notification(db, source, body)

    # Mailgun: has "event-data" key
    if isinstance(body, dict) and "event-data" in body:
        return _handle_mailgun_event(db, source, body)

    # SendGrid: array of event objects
    if isinstance(body, list):
        return _handle_sendgrid_events(db, source, body)

    # Mailgun legacy: has "event" key directly
    if isinstance(body, dict) and "event" in body:
        return _handle_mailgun_legacy(db, source, body)

    logger.warning("Unknown email feedback payload format for %s %s", source_label, source.id)
    return {"status": "ignored", "reason": "unknown_format"}


@router.post("/{instance_id}/feedback")
async def email_feedback_webhook(
    instance_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Receive email delivery feedback (bounces, complaints) from any provider.

    Auto-detects SES SNS, Mailgun, or SendGrid payload format.
    """
    instance = db.query(EmailInstance).filter(EmailInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    try:
        body = await request.json()
    except Exception:
        body = dict(await request.form())

    return await _process_feedback(db, instance, body, "instance")


@router.post("/smtp-config/{config_id}/feedback")
async def smtp_feedback_webhook(
    config_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Receive email delivery feedback (bounces, complaints) for an SMTP config.

    Auto-detects SES SNS, Mailgun, or SendGrid payload format.
    """
    config = db.query(CustomerSMTPConfig).filter(CustomerSMTPConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="SMTP config not found")

    try:
        body = await request.json()
    except Exception:
        body = dict(await request.form())

    return await _process_feedback(db, config, body, "smtp-config")


async def _handle_sns_subscription_confirmation(body: dict) -> dict:
    """Auto-confirm SNS subscription by visiting the SubscribeURL."""
    subscribe_url = body.get("SubscribeURL")
    if subscribe_url:
        async with httpx.AsyncClient() as client:
            await client.get(subscribe_url)
        logger.info("SNS subscription confirmed: %s", body.get("TopicArn"))
    return {"status": "confirmed"}


def _handle_ses_notification(db: Session, source, body: dict) -> dict:
    """Process SES bounce/complaint/delivery notification."""
    from app.services.channels.delivery_feedback_service import DeliveryFeedbackService

    message_str = body.get("Message", "{}")
    if isinstance(message_str, str):
        message = json.loads(message_str)
    else:
        message = message_str

    notification_type = message.get("notificationType", "").lower()
    svc = DeliveryFeedbackService(db)
    project_id = source.project_id
    count = 0

    # SES message IDs: mail.messageId is SES-assigned, commonHeaders.messageId
    # is the Message-ID header we set (used as provider_message_id in SendLog for SMTP sends)
    mail_obj = message.get("mail", {})
    ses_message_id = mail_obj.get("messageId")
    common_message_id = None
    common_headers = mail_obj.get("commonHeaders", {})
    if common_headers.get("messageId"):
        common_message_id = common_headers["messageId"]

    if notification_type == "bounce":
        bounce = message.get("bounce", {})
        bounce_type = bounce.get("bounceType", "").lower()  # Permanent, Transient, Undetermined
        feedback_type = "hard_bounce" if bounce_type == "permanent" else "soft_bounce"

        for recipient in bounce.get("bouncedRecipients", []):
            email = recipient.get("emailAddress", "")
            diag = recipient.get("diagnosticCode", "")
            status_code = recipient.get("status", "")

            send_log_id = _resolve_send_log_id(db, ses_message_id, common_message_id)

            svc.record_feedback(
                project_id=project_id, channel="email", recipient=email,
                feedback_type=feedback_type, reason=diag[:500] if diag else bounce.get("bounceSubType"),
                provider="ses", provider_code=status_code,
                provider_detail=diag, send_log_id=send_log_id,
                raw_payload=message,
            )
            count += 1

    elif notification_type == "complaint":
        complaint = message.get("complaint", {})
        for recipient in complaint.get("complainedRecipients", []):
            email = recipient.get("emailAddress", "")
            send_log_id = _resolve_send_log_id(db, ses_message_id, common_message_id)

            svc.record_feedback(
                project_id=project_id, channel="email", recipient=email,
                feedback_type="complaint",
                reason=complaint.get("complaintSubType", "spam_complaint"),
                provider="ses", provider_code="complaint",
                send_log_id=send_log_id, raw_payload=message,
            )
            count += 1

    elif notification_type == "delivery":
        # Successful delivery — update SendLog via DeliveryTracker
        from app.services.channels.delivery_tracker import DeliveryTracker
        tracker = DeliveryTracker(db)
        # Try SES-assigned ID first, then our Message-ID header
        matched = tracker.record_status(
            provider_message_id=ses_message_id or "",
            status="delivered",
            provider_status="delivered",
        )
        if not matched and common_message_id:
            # SMTP sends store Message-ID header as provider_message_id
            tracker.record_status(
                provider_message_id=common_message_id,
                status="delivered",
                provider_status="delivered",
            )

    db.commit()
    return {"status": "processed", "count": count}


def _handle_mailgun_event(db: Session, source, body: dict) -> dict:
    """Process Mailgun event webhook."""
    from app.services.channels.delivery_feedback_service import DeliveryFeedbackService

    event_data = body.get("event-data", {})
    event_type = event_data.get("event", "").lower()
    svc = DeliveryFeedbackService(db)

    type_map = {
        "bounced": "hard_bounce",
        "failed": "soft_bounce",
        "complained": "complaint",
        "unsubscribed": "complaint",
    }

    feedback_type = type_map.get(event_type)
    if not feedback_type:
        return {"status": "ignored", "event": event_type}

    recipient = event_data.get("recipient", "")
    reason = event_data.get("delivery-status", {}).get("description", "")
    code = event_data.get("delivery-status", {}).get("code", "")
    message_id = event_data.get("message", {}).get("headers", {}).get("message-id")
    send_log_id = _resolve_send_log_id(db, message_id) if message_id else None

    svc.record_feedback(
        project_id=source.project_id, channel="email", recipient=recipient,
        feedback_type=feedback_type, reason=reason,
        provider="mailgun", provider_code=str(code),
        send_log_id=send_log_id, raw_payload=body,
    )
    db.commit()
    return {"status": "processed"}


def _handle_mailgun_legacy(db: Session, source, body: dict) -> dict:
    """Process Mailgun legacy webhook format."""
    from app.services.channels.delivery_feedback_service import DeliveryFeedbackService

    event_type = body.get("event", "").lower()
    type_map = {"bounced": "hard_bounce", "dropped": "soft_bounce", "complained": "complaint"}
    feedback_type = type_map.get(event_type)
    if not feedback_type:
        return {"status": "ignored"}

    svc = DeliveryFeedbackService(db)
    svc.record_feedback(
        project_id=source.project_id, channel="email",
        recipient=body.get("recipient", ""),
        feedback_type=feedback_type, reason=body.get("error", body.get("description", "")),
        provider="mailgun", provider_code=body.get("code"),
        raw_payload=body,
    )
    db.commit()
    return {"status": "processed"}


def _handle_sendgrid_events(db: Session, source, events: list) -> dict:
    """Process SendGrid event webhook (array of events)."""
    from app.services.channels.delivery_feedback_service import DeliveryFeedbackService

    svc = DeliveryFeedbackService(db)
    count = 0

    type_map = {
        "bounce": "hard_bounce",
        "blocked": "soft_bounce",
        "spamreport": "complaint",
        "unsubscribe": "complaint",
        "deferred": "soft_bounce",
    }

    for event in events:
        event_type = event.get("event", "").lower()
        feedback_type = type_map.get(event_type)
        if not feedback_type:
            continue

        # Distinguish hard/soft for SendGrid bounces
        if event_type == "bounce":
            bounce_classification = event.get("type", "")
            if bounce_classification == "blocked":
                feedback_type = "soft_bounce"

        email = event.get("email", "")
        send_log_id = _resolve_send_log_id(db, event.get("sg_message_id"))

        svc.record_feedback(
            project_id=source.project_id, channel="email", recipient=email,
            feedback_type=feedback_type, reason=event.get("reason", ""),
            provider="sendgrid", provider_code=event.get("status", ""),
            send_log_id=send_log_id, raw_payload=event,
        )
        count += 1

    db.commit()
    return {"status": "processed", "count": count}


def _resolve_send_log_id(db: Session, provider_message_id: Optional[str], common_message_id: Optional[str] = None) -> Optional[int]:
    """Resolve SendLog.id from a provider message ID or Message-ID header.

    SES SMTP sends use the email Message-ID header as provider_message_id in SendLog,
    while SES SNS notifications report the SES-assigned ID in mail.messageId.
    We try the SES ID first, then fall back to the Message-ID header from commonHeaders.
    """
    for mid in [provider_message_id, common_message_id]:
        if not mid:
            continue
        # Try with and without angle brackets
        clean_id = mid.strip("<>")
        log = db.query(SendLog.id).filter(
            SendLog.provider_message_id == clean_id,
        ).first()
        if log:
            return log.id
        log = db.query(SendLog.id).filter(
            SendLog.provider_message_id == mid,
        ).first()
        if log:
            return log.id
    return None
