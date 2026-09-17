"""
Approval Queue Router

Endpoints for reviewing and acting on debug-mode funnel messages
that require attendant approval before being sent.
"""

import logging
import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.models import SupportTicket, ChatMessage, FunnelEnrollment, FunnelEnrollmentLog, FunnelStep, MessagingUser
from app.services.support_inbox_service import SupportInboxService
from app.services.realtime.redis_pubsub import publish_inbox_event
from app.services.realtime import event_types as rt

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/approvals",
    tags=["approvals"],
)


class ApprovalListItem(BaseModel):
    ticket_id: int
    ticket_number: str
    message_id: int
    message_content: str
    funnel_id: int
    funnel_name: str
    step_id: int
    enrollment_id: int
    contact_identifier: str
    created_at: str

    class Config:
        from_attributes = True


class EditApprovalRequest(BaseModel):
    message: str


@router.get("")
def list_pending_approvals(project_id: int, db: Session = Depends(get_db)):
    """List all pending approval requests for a project."""
    tickets = (
        db.query(SupportTicket)
        .filter(
            SupportTicket.project_id == project_id,
            SupportTicket.tags.contains(["approval_queue"]),
            SupportTicket.status.in_(["open", "in_progress"]),
        )
        .order_by(SupportTicket.created_at.desc())
        .all()
    )

    results = []
    for ticket in tickets:
        origin = ticket.escalation_origin or {}
        message_id = origin.get("message_id")
        if not message_id:
            continue

        msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
        if not msg or msg.delivery_status != "pending_approval":
            continue

        enrollment_id = origin.get("ref_id")
        enrollment = db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
        ).first() if enrollment_id else None

        results.append({
            "ticket_id": ticket.id,
            "ticket_number": ticket.ticket_number,
            "message_id": msg.id,
            "message_content": msg.content,
            "funnel_id": enrollment.funnel_id if enrollment else None,
            "funnel_name": enrollment.funnel.name if enrollment and enrollment.funnel else "",
            "step_id": origin.get("step_id"),
            "enrollment_id": enrollment_id,
            "contact_identifier": ticket.customer_identifier,
            "created_at": ticket.created_at.isoformat(),
        })

    return results


@router.get("/{ticket_id}/preview")
def preview_message(project_id: int, ticket_id: int, db: Session = Depends(get_db)):
    """Render and return the full message preview for an approval ticket.

    Returns JSON with rendered HTML (for email) or plain text, plus subject and channel.
    """
    ticket = db.query(SupportTicket).filter(
        SupportTicket.id == ticket_id,
        SupportTicket.project_id == project_id,
    ).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    origin = ticket.escalation_origin or {}
    if origin.get("type") != "funnel_approval":
        raise HTTPException(status_code=400, detail="Not an approval ticket")

    message_id = origin.get("message_id")
    msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    send_config = (msg.message_metadata or {}).get("send_config", {})
    channel = send_config.get("channel", "whatsapp")

    # If already rendered (new tickets), return stored HTML
    if send_config.get("html"):
        # Resolve body_format: prefer stored value, fall back to template lookup
        body_format = send_config.get("body_format")
        if not body_format and send_config.get("template_id"):
            from app.models.messaging import MessagingTemplate
            tpl = db.query(MessagingTemplate).filter(
                MessagingTemplate.id == send_config["template_id"],
                MessagingTemplate.project_id == project_id,
            ).first()
            body_format = getattr(tpl, "body_format", None) if tpl else None
        body_format = body_format or "html"

        html_content = send_config["html"]
        # Match email_service.py logic: convert \n to <br> for plain text content
        is_plain = body_format == "plain" or (
            not re.search(r'<[a-z][\s\S]*>', html_content, re.IGNORECASE)
        )
        if is_plain:
            html_content = html_content.replace('\n', '<br>\n')

        return {
            "channel": channel,
            "subject": send_config.get("subject", ""),
            "html": html_content,
            "text": msg.content,
            "body_format": body_format,
        }

    # WhatsApp: no on-the-fly rendering (pre-change tickets have no stored HTML)
    if channel == "whatsapp":
        return {
            "channel": channel,
            "subject": "",
            "html": "",
            "text": msg.content or send_config.get("template_name", "WhatsApp message"),
        }

    # Otherwise render on-the-fly from template_id (email)
    enrollment_id = origin.get("ref_id")
    rendered = _render_email_from_config(db, send_config, enrollment_id, project_id)

    return {
        "channel": channel,
        "subject": rendered.get("subject", ""),
        "html": rendered.get("html", ""),
        "text": rendered.get("text", msg.content),
        "body_format": rendered.get("body_format", "html"),
    }


@router.post("/{ticket_id}/approve")
async def approve_message(project_id: int, ticket_id: int, db: Session = Depends(get_db)):
    """Approve a pending message and send it."""
    ticket = db.query(SupportTicket).filter(
        SupportTicket.id == ticket_id,
        SupportTicket.project_id == project_id,
    ).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    origin = ticket.escalation_origin or {}
    if origin.get("type") != "funnel_approval":
        raise HTTPException(status_code=400, detail="Not an approval ticket")

    message_id = origin.get("message_id")
    msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not msg or msg.delivery_status != "pending_approval":
        raise HTTPException(status_code=400, detail="Message already processed")

    enrollment_id = origin.get("ref_id")
    step_id = origin.get("step_id")

    # Send the message
    send_config = (msg.message_metadata or {}).get("send_config", {})
    send_result = {}
    try:
        send_result = await _execute_approved_send(db, msg.content, send_config, ticket) or {}
        msg.delivery_status = "sent"
    except Exception as e:
        logger.error(f"Failed to send approved message: {e}")
        msg.delivery_status = "failed"
        # Log the failure to enrollment
        _write_approval_log(
            db, enrollment_id, step_id, "approval_send_failed",
            ticket.id, message_id, extra={"error": str(e)},
        )
        db.commit()
        raise HTTPException(status_code=500, detail=f"Send failed: {str(e)}")

    # Resolve ticket
    ticket.status = "resolved"
    from datetime import datetime
    ticket.resolved_at = datetime.utcnow()
    _add_ticket_tag(ticket, "approval_approved")
    _write_approval_log(
        db, enrollment_id, step_id, "approval_approved",
        ticket.id, message_id, extra=send_result,
    )

    # Increment messages_sent on the enrollment
    if enrollment_id:
        _enr = db.query(FunnelEnrollment).filter(FunnelEnrollment.id == enrollment_id).first()
        if _enr:
            _enr.messages_sent = (_enr.messages_sent or 0) + 1

    # Resume enrollment
    _resume_after_approval(
        db, enrollment_id, step_id, send_config,
        thread_index=origin.get("thread_index"),
    )

    db.commit()

    await publish_inbox_event(project_id, rt.TICKET_UPDATED, {
        "id": ticket.id, "project_id": project_id,
        "ticket_number": ticket.ticket_number, "status": ticket.status,
    })

    return {"success": True, "action": "approved", "ticket_id": ticket_id}


@router.post("/{ticket_id}/reject")
async def reject_message(project_id: int, ticket_id: int, db: Session = Depends(get_db)):
    """Reject a pending message — it will not be sent."""
    ticket = db.query(SupportTicket).filter(
        SupportTicket.id == ticket_id,
        SupportTicket.project_id == project_id,
    ).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    origin = ticket.escalation_origin or {}
    if origin.get("type") != "funnel_approval":
        raise HTTPException(status_code=400, detail="Not an approval ticket")

    message_id = origin.get("message_id")
    msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if msg:
        msg.delivery_status = "rejected"

    ticket.status = "resolved"
    from datetime import datetime
    ticket.resolved_at = datetime.utcnow()
    _add_ticket_tag(ticket, "approval_rejected")

    step_id = origin.get("step_id")
    _write_approval_log(db, origin.get("ref_id"), step_id, "approval_rejected", ticket.id, message_id)

    # Mark enrollment approval as rejected (enrollment stays paused)
    enrollment_id = origin.get("ref_id")
    if enrollment_id:
        enrollment = db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
        ).first()
        if enrollment:
            from sqlalchemy.orm.attributes import flag_modified
            meta = dict(enrollment.enrollment_metadata or {})
            meta["_approval_paused"] = False
            meta["_approval_rejected"] = True
            enrollment.enrollment_metadata = meta
            flag_modified(enrollment, "enrollment_metadata")

    db.commit()

    await publish_inbox_event(project_id, rt.TICKET_UPDATED, {
        "id": ticket.id, "project_id": project_id,
        "ticket_number": ticket.ticket_number, "status": ticket.status,
    })

    return {"success": True, "action": "rejected", "ticket_id": ticket_id}


@router.put("/{ticket_id}/edit")
async def edit_and_approve(
    project_id: int, ticket_id: int,
    body: EditApprovalRequest,
    db: Session = Depends(get_db),
):
    """Edit the message content and then approve & send it."""
    ticket = db.query(SupportTicket).filter(
        SupportTicket.id == ticket_id,
        SupportTicket.project_id == project_id,
    ).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    origin = ticket.escalation_origin or {}
    if origin.get("type") != "funnel_approval":
        raise HTTPException(status_code=400, detail="Not an approval ticket")

    message_id = origin.get("message_id")
    msg = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not msg or msg.delivery_status != "pending_approval":
        raise HTTPException(status_code=400, detail="Message already processed")

    # Update message content
    msg.content = body.message

    enrollment_id = origin.get("ref_id")
    step_id = origin.get("step_id")

    # Send the edited message
    send_config = (msg.message_metadata or {}).get("send_config", {})
    send_result = {}
    try:
        send_result = await _execute_approved_send(db, body.message, send_config, ticket) or {}
        msg.delivery_status = "sent"
    except Exception as e:
        logger.error(f"Failed to send edited message: {e}")
        msg.delivery_status = "failed"
        _write_approval_log(
            db, enrollment_id, step_id, "approval_send_failed",
            ticket.id, message_id, extra={"error": str(e)},
        )
        db.commit()
        raise HTTPException(status_code=500, detail=f"Send failed: {str(e)}")

    # Resolve ticket
    ticket.status = "resolved"
    from datetime import datetime
    ticket.resolved_at = datetime.utcnow()
    _add_ticket_tag(ticket, "approval_approved")
    _write_approval_log(
        db, enrollment_id, step_id, "approval_approved",
        ticket.id, message_id, extra=send_result,
    )

    # Increment messages_sent on the enrollment
    if enrollment_id:
        _enr = db.query(FunnelEnrollment).filter(FunnelEnrollment.id == enrollment_id).first()
        if _enr:
            _enr.messages_sent = (_enr.messages_sent or 0) + 1

    # Resume enrollment
    _resume_after_approval(
        db, enrollment_id, step_id, send_config,
        thread_index=origin.get("thread_index"),
    )

    db.commit()

    await publish_inbox_event(project_id, rt.TICKET_UPDATED, {
        "id": ticket.id, "project_id": project_id,
        "ticket_number": ticket.ticket_number, "status": ticket.status,
    })

    return {"success": True, "action": "edited_and_approved", "ticket_id": ticket_id}


# -- Helpers --

def _add_ticket_tag(ticket: SupportTicket, tag: str) -> None:
    """Append a tag to the ticket's tags array (new list for SQLAlchemy change detection)."""
    ticket.tags = list(ticket.tags or []) + [tag]


def _write_approval_log(
    db: Session, enrollment_id: Optional[int], step_id: Optional[int],
    action: str, ticket_id: int, message_id: int,
    extra: Optional[dict] = None,
) -> None:
    """Write an approval log entry with optional send result details."""
    if not enrollment_id:
        return
    details: dict = {"ticket_id": ticket_id, "message_id": message_id}
    if extra:
        details.update(extra)
    log = FunnelEnrollmentLog(
        enrollment_id=enrollment_id,
        step_id=step_id,
        action=action,
        details=details,
    )
    db.add(log)


def _render_email_from_config(
    db: Session, send_config: dict, enrollment_id: Optional[int], project_id: int,
) -> dict:
    """Render the email template from send_config + enrollment context.

    Returns dict with 'html', 'subject', 'text'.
    """
    from app.services.messaging.template_renderer import template_renderer

    template_id = send_config.get("template_id")
    message_type = send_config.get("message_type", "template")

    # Build variable context from enrollment
    variables = {}
    enrollment = None
    if enrollment_id:
        enrollment = db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
        ).first()

    if enrollment and enrollment.user_id:
        user = db.query(MessagingUser).filter(
            MessagingUser.id == enrollment.user_id,
        ).first()
        if user:
            if user.name:
                variables["name"] = user.name
                variables["first_name"] = user.name.split()[0]
            if user.email:
                variables["email"] = user.email
            if user.phone:
                variables["phone"] = user.phone
            if user.external_id:
                variables["external_id"] = user.external_id
            for k, v in (user.properties or {}).items():
                variables[k] = v

    if enrollment:
        for k, v in (enrollment.enrollment_metadata or {}).items():
            if not k.startswith("_") and k != "events":
                variables[k] = v
        if enrollment.enrollment_metadata and "events" in enrollment.enrollment_metadata:
            variables["events"] = enrollment.enrollment_metadata["events"]

    # Apply variable_mapping
    variable_mapping = send_config.get("variable_mapping", {})
    for tpl_var, source_path in variable_mapping.items():
        resolved = _resolve_variable_path(variables, source_path)
        if resolved is not None:
            variables[tpl_var] = resolved

    subject = ""
    html_body = ""
    body_format = "html"

    if message_type == "template" and template_id:
        from app.models.messaging import MessagingTemplate
        tpl = db.query(MessagingTemplate).filter(
            MessagingTemplate.id == template_id,
            MessagingTemplate.project_id == project_id,
        ).first()
        if tpl:
            subject = tpl.subject or ""
            html_body = tpl.body or ""
            body_format = getattr(tpl, 'body_format', None) or "html"
        else:
            return {"html": "", "subject": "", "text": f"Template #{template_id} not found"}
    else:
        subject = send_config.get("subject", "")
        html_body = send_config.get("message", "")

    # Render variables
    html_body, subject, _, _ = template_renderer.render_template(
        html_body or "", variables, subject or None,
    )

    # Match email_service.py logic: convert \n to <br> for plain text content
    is_plain = body_format == "plain" or (
        body_format is None and not re.search(r'<[a-z][\s\S]*>', html_body or "", re.IGNORECASE)
    )
    if is_plain and html_body:
        html_body = html_body.replace('\n', '<br>\n')

    # Plain text preview
    text_preview = re.sub(r"<[^>]+>", "", html_body or "")
    text_preview = re.sub(r"\s+", " ", text_preview).strip()

    return {"html": html_body, "subject": subject, "text": text_preview, "body_format": body_format}


def _resolve_variable_path(variables: dict, path: str):
    """Resolve a dotted path like 'events.auth_signup.email' from variables dict."""
    parts = path.split(".")
    current = variables
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


async def _execute_approved_send(
    db: Session, message_content: str, send_config: dict, ticket: SupportTicket,
) -> dict:
    """Send the approved message via the full SendService pipeline.

    This ensures pixel injection, decision trace, content_payload, and delivery
    tracking are all captured — identical to funnel/event-action sends.
    """
    from app.services.channels.base import OutboundContent
    from app.services.channels.send_service import SendService

    channel = send_config.get("channel", "whatsapp")
    instance_id = send_config.get("instance_id")
    svc = SendService(db)

    # Resolve user_id from the linked enrollment (via escalation_origin)
    user_id = None
    enrollment_id = (ticket.escalation_origin or {}).get("ref_id")
    if enrollment_id:
        enrollment = db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
        ).first()
        if enrollment:
            user_id = enrollment.user_id

    if channel == "whatsapp":
        to_number = ticket.customer_identifier
        message_type = send_config.get("message_type", "text")

        if message_type == "template":
            content = OutboundContent(
                content_type="template",
                text=message_content,
                template_name=send_config.get("template_name", ""),
                template_language=send_config.get("template_language", "en_US"),
                template_components=send_config.get("template_components"),
            )
        else:
            content = OutboundContent(content_type="text", text=message_content)

        instance_cfg = {"instance_id": instance_id} if instance_id else None
        decision = await svc.send(
            project_id=ticket.project_id,
            user_id=user_id,
            recipient=to_number,
            content=content,
            channel="whatsapp",
            source_type="approval",
            source_id=ticket.id,
            instance_config=instance_cfg,
            template_id=send_config.get("template_id"),
        )
        if not decision.success:
            raise ValueError(f"WhatsApp send failed: {decision.error}")

        return {
            "channel": "whatsapp",
            "message_type": message_type,
            "to_number": to_number,
            "send_log_id": decision.send_log_id,
            "template_name": send_config.get("template_name"),
        }

    elif channel == "email":
        recipient_email = _resolve_email_recipient(db, send_config, ticket)
        if not recipient_email:
            raise ValueError("Cannot resolve email recipient for approval send")

        content = OutboundContent(
            content_type="rich" if send_config.get("html") else "text",
            text=message_content,
            html=send_config.get("html"),
            subject=send_config.get("subject"),
        )
        instance_cfg = {
            k: send_config[k]
            for k in ("from_email", "from_name", "reply_to", "send_via")
            if k in send_config
        }
        decision = await svc.send(
            project_id=ticket.project_id,
            user_id=user_id,
            recipient=recipient_email,
            content=content,
            channel="email",
            source_type="approval",
            source_id=ticket.id,
            instance_config=instance_cfg,
            template_id=send_config.get("template_id"),
        )
        if not decision.success:
            raise ValueError(f"Email send failed: {decision.error}")

        return {"channel": "email", "recipient": recipient_email, "send_log_id": decision.send_log_id}

    elif channel == "sms":
        content = OutboundContent(content_type="text", text=message_content)
        decision = await svc.send(
            project_id=ticket.project_id,
            user_id=user_id,
            recipient=ticket.customer_identifier,
            content=content,
            channel="sms",
            source_type="approval",
            source_id=ticket.id,
        )
        if not decision.success:
            raise ValueError(f"SMS send failed: {decision.error}")

        return {"channel": "sms", "recipient": ticket.customer_identifier, "send_log_id": decision.send_log_id}

    else:
        raise ValueError(f"Unsupported channel: '{channel}'")


def _resolve_email_recipient(
    db: Session, send_config: dict, ticket: SupportTicket,
) -> Optional[str]:
    """Resolve the email address for an approved email send.

    Priority: recipient_field on user record → user.email → ticket.customer_identifier (if @).
    """
    enrollment_id = (ticket.escalation_origin or {}).get("ref_id")
    if enrollment_id:
        enrollment = db.query(FunnelEnrollment).filter(
            FunnelEnrollment.id == enrollment_id,
        ).first()
        if enrollment and enrollment.user_id:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == enrollment.user_id,
            ).first()
            if user:
                # Try recipient_field from config (e.g. a custom property)
                recipient_field = send_config.get("recipient_field")
                if recipient_field and recipient_field != "email":
                    props = user.properties or {}
                    field_val = props.get(recipient_field)
                    if field_val and "@" in str(field_val):
                        return str(field_val)
                # Default to user.email
                if user.email:
                    return user.email

    # Fallback: ticket.customer_identifier if it looks like an email
    if ticket.customer_identifier and "@" in ticket.customer_identifier:
        return ticket.customer_identifier

    return None


def _resume_after_approval(
    db: Session, enrollment_id: Optional[int], step_id: Optional[int],
    send_config: dict, thread_index: Optional[int] = None,
) -> None:
    """Resume the funnel enrollment after an approval action."""
    if not enrollment_id:
        return

    enrollment = db.query(FunnelEnrollment).filter(
        FunnelEnrollment.id == enrollment_id,
        FunnelEnrollment.status == "active",
    ).first()
    if not enrollment:
        return

    from sqlalchemy.orm.attributes import flag_modified
    from app.models import FunnelEnrollmentThread

    meta = dict(enrollment.enrollment_metadata or {})
    meta["_approval_paused"] = False
    enrollment.enrollment_metadata = meta
    flag_modified(enrollment, "enrollment_metadata")

    # Recover thread context if this step was inside a fork
    thread = None
    if thread_index is not None:
        thread = next(
            (t for t in enrollment.threads if t.thread_index == thread_index and t.status == "active"),
            None,
        )

    # Check if the step expects a reply (conversation mode)
    handoff_type = send_config.get("handoff_type", "none")
    expect_reply = send_config.get("expect_reply", False)

    if expect_reply and handoff_type != "none":
        # Set up routing state for the handoff (same as normal flow would do)
        from app.services.funnel_engine import FunnelEngine
        engine = FunnelEngine(db)
        step = db.query(FunnelStep).filter(FunnelStep.id == step_id).first() if step_id else None
        if step:
            config = step.step_config or {}
            engine._reestablish_send_routing(enrollment, step, config)
    else:
        # Notification mode — advance to next step
        from app.services.funnel_engine import FunnelEngine
        engine = FunnelEngine(db)
        step = db.query(FunnelStep).filter(FunnelStep.id == step_id).first() if step_id else enrollment.current_step
        if step:
            engine._notification_advance(enrollment, step, thread=thread)
