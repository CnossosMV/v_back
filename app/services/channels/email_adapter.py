"""
Email Channel Adapter — wraps EmailService (SMTP) + WebhookDispatcher + EmailInstance.
"""
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.services.channels.base import (
    ChannelAdapter, ChannelConstraints, OutboundContent, SendResult,
)

logger = logging.getLogger(__name__)


class EmailAdapter(ChannelAdapter):

    @property
    def channel_name(self) -> str:
        return "email"

    @staticmethod
    def _resolve_instance(
        db: Session,
        project_id: int,
        instance_config: Optional[Dict[str, Any]] = None,
    ):
        """Find the EmailInstance to use (if any)."""
        from sqlalchemy import case, or_

        from app.models import EmailInstance, Project

        cfg = instance_config or {}
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return None

        if cfg.get("instance_id"):
            return db.query(EmailInstance).filter(
                EmailInstance.id == cfg["instance_id"],
                EmailInstance.is_active == True,
                EmailInstance.workspace_id == project.workspace_id,
                or_(
                    EmailInstance.project_id == project_id,
                    EmailInstance.project_id.is_(None),
                ),
            ).first()

        # Backward-compatible fallback, deterministic and project-specific first.
        return db.query(EmailInstance).filter(
            EmailInstance.workspace_id == project.workspace_id,
            EmailInstance.is_active == True,
            or_(
                EmailInstance.project_id == project_id,
                EmailInstance.project_id.is_(None),
            ),
        ).order_by(
            case((EmailInstance.project_id == project_id, 0), else_=1),
            EmailInstance.id,
        ).first()

    async def send(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        try:
            cfg = {**(content.metadata or {}), **(instance_config or {})}
            cfg.setdefault("project_id", project_id)

            # 1. Try EmailInstance (new path), unless the caller explicitly
            # selected the project's legacy SMTP configuration. The choice of
            # transport remains inside the Send Layer; this flag does not
            # bypass policy, Selection, Guardian or SendLog creation.
            instance = None
            if not cfg.get("smtp_config_id") and not cfg.get("use_direct_smtp"):
                instance = self._resolve_instance(db, project_id, cfg)
            if instance:
                return await self._send_via_instance(db, instance, recipient, content, cfg)
            if cfg.get("instance_id") and not cfg.get("use_direct_smtp"):
                return SendResult(
                    success=False,
                    error="Email instance not found for this project",
                )

            # 2. Webhook path
            send_via = cfg.get("send_via", "smtp")
            if send_via == "webhook" and cfg.get("channel_id"):
                return await self._send_via_webhook(db, project_id, recipient, content, cfg)

            # 3. Legacy SMTP path (CustomerSMTPConfig)
            return self._send_via_smtp(db, project_id, recipient, content, cfg)
        except Exception as e:
            logger.error(f"Email send error: {e}", exc_info=True)
            return SendResult(success=False, error=str(e))

    def check_constraints(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> ChannelConstraints:
        if not recipient or "@" not in recipient:
            return ChannelConstraints(available=False, reason="Invalid email address")

        cfg = instance_config or {}

        if cfg.get("smtp_config_id"):
            from app.models import CustomerSMTPConfig
            smtp_config = db.query(CustomerSMTPConfig).filter(
                CustomerSMTPConfig.id == cfg["smtp_config_id"],
                CustomerSMTPConfig.project_id == project_id,
                CustomerSMTPConfig.is_active == True,
            ).first()
            if not smtp_config:
                return ChannelConstraints(
                    available=False,
                    reason="SMTP configuration not found for this project",
                )
            return ChannelConstraints(available=True)

        # Check if there's an EmailInstance available for this project unless
        # the caller explicitly requested the project's SMTP configuration.
        if cfg.get("instance_id") and not cfg.get("use_direct_smtp"):
            inst = self._resolve_instance(db, project_id, cfg)
            if inst:
                if inst.connection_status == "failed":
                    return ChannelConstraints(available=False, reason="Email instance connection failed")
                return ChannelConstraints(available=True)
            return ChannelConstraints(available=False, reason="Email instance not found for this project")

        send_via = cfg.get("send_via", "smtp")

        if send_via == "smtp":
            # Check if project has SMTP config or system-level SMTP
            from app.models import CustomerSMTPConfig
            has_config = db.query(CustomerSMTPConfig).filter(
                CustomerSMTPConfig.project_id == project_id
            ).first()
            if not has_config:
                import os
                if not os.getenv("EMAIL_HOST"):
                    # One more check: is there a workspace-level EmailInstance?
                    instance = self._resolve_instance(db, project_id)
                    if instance:
                        return ChannelConstraints(available=True)
                    return ChannelConstraints(available=False, reason="No SMTP configuration")

        return ChannelConstraints(available=True)

    def negotiate_content(
        self,
        content: OutboundContent,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> OutboundContent:
        # Wrap plain text in basic HTML if no html provided
        if content.text and not content.html:
            escaped = (
                content.text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            content.html = f"<html><body><p>{escaped}</p></body></html>"
        return content

    # ── helpers ──────────────────────────────────────────────────────────

    async def _send_via_instance(
        self,
        db: Session,
        instance,
        recipient: str,
        content: OutboundContent,
        cfg: Dict[str, Any],
    ) -> SendResult:
        import os
        import uuid
        from app.services.email_sender import EmailSender

        subject = content.subject or "Message"
        html_body = content.html or content.text or ""
        from_email = cfg.get("from_email")
        from_name = cfg.get("from_name")
        reply_to = cfg.get("reply_to")

        extra_headers = {}
        unsub_user_id = cfg.get("user_id")
        if unsub_user_id:
            from app.routers.unsubscribe import generate_unsubscribe_token
            api_base = os.getenv("API_BASE_URL", "").rstrip("/")
            if api_base:
                unsub_token = generate_unsubscribe_token(int(cfg["project_id"]), unsub_user_id, "email")
                unsub_url = f"{api_base}/unsubscribe/{unsub_token}"
                extra_headers["List-Unsubscribe"] = f"<{unsub_url}>"
                extra_headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
                if cfg.get("policy_profile") == "campaign" and unsub_url not in html_body:
                    locale = str(cfg.get("locale") or "").lower()
                    label = "Cancelar inscrição" if locale.startswith("pt") else "Unsubscribe"
                    html_body += (
                        '<hr style="border:0;border-top:1px solid #ddd;margin-top:24px">'
                        f'<p style="font-size:12px;color:#666"><a href="{unsub_url}">{label}</a></p>'
                    )

        # Inject reply-tracking address when inbound domain is configured
        inbound_domain = os.getenv("EMAIL_INBOUND_DOMAIN")
        conversation_id = cfg.get("conversation_id") or cfg.get("send_log_id")
        if inbound_domain and not reply_to and conversation_id:
            reply_to = f"reply+{conversation_id}@{inbound_domain}"

        msg_id_domain = inbound_domain or "versya.io"
        generated_message_id = cfg.get("message_id") or f"<{uuid.uuid4().hex}@{msg_id_domain}>"

        sender = EmailSender(db)
        result = await sender.send_email(
            instance=instance,
            to_email=recipient,
            subject=subject,
            html_body=html_body,
            from_email=from_email,
            from_name=from_name,
            reply_to=reply_to,
            message_id=generated_message_id,
            attachments=content.attachments,
            extra_headers=extra_headers or None,
        )
        return SendResult(
            success=result.get("success", False),
            provider_message_id=result.get("provider_message_id"),
            error=result.get("message") if not result.get("success") else None,
            error_code=result.get("error_code"),
            provider_response=result,
            instance_id=instance.id,
        )

    def _send_via_smtp(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        cfg: Dict[str, Any],
    ) -> SendResult:
        import os
        import uuid
        from app.services.email_service import EmailService

        subject = content.subject or "Message"
        html_body = content.html or content.text or ""
        from_email = cfg.get("from_email")
        from_name = cfg.get("from_name")
        reply_to = cfg.get("reply_to")

        # Inject reply-tracking address when inbound domain is configured
        inbound_domain = os.getenv("EMAIL_INBOUND_DOMAIN")
        conversation_id = cfg.get("conversation_id") or cfg.get("send_log_id")
        if inbound_domain and not reply_to and conversation_id:
            reply_to = f"reply+{conversation_id}@{inbound_domain}"

        # Generate Message-ID for threading
        msg_id_domain = inbound_domain or "versya.io"
        generated_message_id = cfg.get("message_id") or f"<{uuid.uuid4().hex}@{msg_id_domain}>"

        if cfg.get("smtp_config_id"):
            email_svc = EmailService.from_smtp_config_id(
                db, cfg["smtp_config_id"], project_id,
            )
        else:
            email_svc = EmailService.from_project_config(db, project_id)

        # List-Unsubscribe headers (RFC 8058)
        extra_headers = {}
        unsub_user_id = cfg.get("user_id")
        if unsub_user_id:
            from app.routers.unsubscribe import generate_unsubscribe_token
            api_base = os.getenv("API_BASE_URL", "").rstrip("/")
            if api_base:
                unsub_token = generate_unsubscribe_token(project_id, unsub_user_id, "email")
                unsub_url = f"{api_base}/unsubscribe/{unsub_token}"
                extra_headers["List-Unsubscribe"] = f"<{unsub_url}>"
                extra_headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
                if cfg.get("policy_profile") == "campaign" and unsub_url not in html_body:
                    locale = str(cfg.get("locale") or "").lower()
                    label = "Cancelar inscrição" if locale.startswith("pt") else "Unsubscribe"
                    html_body += (
                        '<hr style="border:0;border-top:1px solid #ddd;margin-top:24px">'
                        f'<p style="font-size:12px;color:#666"><a href="{unsub_url}">{label}</a></p>'
                    )

        result = email_svc.send_html_email(
            to_email=recipient,
            subject=subject,
            html_body=html_body,
            from_email=from_email,
            from_name=from_name,
            reply_to=reply_to,
            message_id=generated_message_id,
            in_reply_to=cfg.get("in_reply_to"),
            references=cfg.get("references"),
            extra_headers=extra_headers or None,
            attachments=content.attachments,
        )
        return SendResult(
            success=result.get("success", False),
            provider_message_id=result.get("provider_message_id"),
            error=result.get("message") if not result.get("success") else None,
            error_code=result.get("error_code"),
            provider_response=result,
        )

    async def _send_via_webhook(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        cfg: Dict[str, Any],
    ) -> SendResult:
        from app.services.messaging.webhook_dispatcher import WebhookDispatcher

        dispatcher = WebhookDispatcher(db)
        channel_id = cfg["channel_id"]

        payload = {
            "to": recipient,
            "subject": content.subject or "Message",
            "html_body": content.html or content.text or "",
            "from_email": cfg.get("from_email"),
            "from_name": cfg.get("from_name"),
            "reply_to": cfg.get("reply_to"),
            "attachments": content.attachments or [],
            # Stable across campaign retries; webhook receivers can use this
            # as their provider-side idempotency key.
            "message_id": cfg.get("message_id"),
        }

        result = await dispatcher.dispatch(channel_id=channel_id, payload=payload)
        return SendResult(
            success=result.get("success", False),
            provider_message_id=result.get("provider_message_id"),
            error=result.get("error"),
            provider_response=result,
        )
