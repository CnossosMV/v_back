"""
Email Inbound Service — handles incoming email webhooks from SES/Mailgun/SendGrid.

Follows the same pattern as meta_cloud_api_service.py:
1. Parse raw webhook payload (provider-specific)
2. Resolve SMTP instance + project
3. Resolve inbound address
4. Check reply-tracking ID
5. Normalize to InboundMessage
6. Route via InboundRouter
"""
import hashlib
import hmac
import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models import CustomerSMTPConfig, EmailInboundAddress
from app.services.inbound.adapters import normalize_email

logger = logging.getLogger(__name__)

# Pattern for reply-tracking: reply+{id}@domain.com
REPLY_TRACKING_PATTERN = re.compile(r"reply\+(\w+)@")


class EmailInboundService:
    def __init__(self, db: Session):
        self.db = db

    async def handle_webhook(
        self,
        instance_id: int,
        raw_payload: dict,
        provider_type: str,
    ) -> dict:
        """Process an inbound email webhook."""
        # Load SMTP config
        config = self.db.query(CustomerSMTPConfig).filter(
            CustomerSMTPConfig.id == instance_id,
            CustomerSMTPConfig.inbound_enabled == True,
        ).first()
        if not config:
            logger.warning(f"Email inbound: instance {instance_id} not found or inbound not enabled")
            return {"success": False, "error": "instance_not_found"}

        project_id = config.project_id

        # Parse based on provider
        parsed = self._parse_payload(raw_payload, provider_type)
        if not parsed:
            return {"success": False, "error": "parse_failed"}

        # Intercept MAILER-DAEMON bounce/DSN messages
        from_email = (parsed.get("from_email") or "").lower()
        if "mailer-daemon" in from_email or "postmaster" in from_email:
            return self._handle_bounce_dsn(project_id, parsed, raw_payload)

        # Resolve inbound address
        to_addresses = parsed.get("to_addresses", [])
        inbound_addr = None
        inbound_address_id = None
        for addr in to_addresses:
            inbound_addr = self._resolve_inbound_address(instance_id, addr)
            if inbound_addr:
                inbound_address_id = inbound_addr.id
                break

        # Check for reply tracking
        reply_tracking_id = None
        for addr in to_addresses:
            reply_tracking_id = self._extract_reply_tracking_id(addr)
            if reply_tracking_id:
                break

        if reply_tracking_id:
            parsed["reply_tracking_id"] = reply_tracking_id

        # Normalize
        message = normalize_email(
            parsed=parsed,
            provider_type=provider_type,
            instance_id=instance_id,
            project_id=project_id,
            inbound_address_id=inbound_address_id,
        )

        # Route
        from app.services.inbound.router import InboundRouter
        router = InboundRouter(self.db)
        result = await router.route(message)
        return result

    def _parse_payload(self, raw: dict, provider_type: str) -> Optional[dict]:
        """Parse raw webhook payload based on provider type."""
        try:
            if provider_type == "ses_inbound":
                return self._parse_ses_sns(raw)
            elif provider_type == "mailgun_inbound":
                return self._parse_mailgun(raw)
            elif provider_type == "sendgrid_inbound":
                return self._parse_sendgrid(raw)
            elif provider_type == "cloudflare_email_workers":
                return self._parse_cloudflare_worker(raw)
            else:
                logger.warning(f"Unknown email inbound provider: {provider_type}")
                return None
        except Exception as e:
            logger.error(f"Email inbound parse error ({provider_type}): {e}", exc_info=True)
            return None

    def _parse_ses_sns(self, raw: dict) -> Optional[dict]:
        """Parse AWS SES inbound via SNS notification."""
        # SNS wraps the message
        message = raw
        if isinstance(raw.get("Message"), str):
            message = json.loads(raw["Message"])

        mail = message.get("mail", {})
        content = message.get("content", "")

        # Parse email headers from SES
        headers = {h["name"].lower(): h["value"] for h in mail.get("headers", [])}

        from_email = ""
        from_name = None
        source = mail.get("source", "")
        if source:
            from_email = source
            # Try to extract name from common headers
            from_header = headers.get("from", "")
            if "<" in from_header:
                from_name = from_header.split("<")[0].strip().strip('"')
                from_email = from_header.split("<")[1].rstrip(">")

        to_addresses = [d.get("address", "") for d in mail.get("destination", [])]
        if not to_addresses:
            to_header = headers.get("to", "")
            to_addresses = [a.strip() for a in to_header.split(",") if a.strip()]

        # Parse body from content string (simplified — full MIME parsing would use email.parser)
        body_text = content if isinstance(content, str) else ""

        return {
            "from_email": from_email,
            "from_name": from_name,
            "to_addresses": to_addresses,
            "subject": headers.get("subject", mail.get("subject", "")),
            "body_text": body_text,
            "body_html": None,
            "message_id_header": headers.get("message-id"),
            "in_reply_to": headers.get("in-reply-to"),
            "references": headers.get("references"),
            "attachments": [],
            "timestamp": mail.get("timestamp"),
        }

    def _parse_mailgun(self, raw: dict) -> Optional[dict]:
        """Parse Mailgun inbound webhook (form-encoded or JSON)."""
        from_field = raw.get("from", raw.get("sender", ""))
        from_email = from_field
        from_name = None
        if "<" in from_field:
            from_name = from_field.split("<")[0].strip().strip('"')
            from_email = from_field.split("<")[1].rstrip(">")

        to_field = raw.get("To", raw.get("recipient", ""))
        to_addresses = [a.strip() for a in to_field.split(",") if a.strip()]

        return {
            "from_email": from_email,
            "from_name": from_name,
            "to_addresses": to_addresses,
            "subject": raw.get("subject", ""),
            "body_text": raw.get("body-plain", raw.get("stripped-text", "")),
            "body_html": raw.get("body-html", raw.get("stripped-html")),
            "message_id_header": raw.get("Message-Id"),
            "in_reply_to": raw.get("In-Reply-To"),
            "references": raw.get("References"),
            "attachments": [],
            "timestamp": raw.get("timestamp"),
        }

    def _parse_sendgrid(self, raw: dict) -> Optional[dict]:
        """Parse SendGrid Inbound Parse webhook."""
        from_field = raw.get("from", "")
        from_email = from_field
        from_name = None
        if "<" in from_field:
            from_name = from_field.split("<")[0].strip().strip('"')
            from_email = from_field.split("<")[1].rstrip(">")

        to_field = raw.get("to", "")
        to_addresses = [a.strip() for a in to_field.split(",") if a.strip()]

        # Parse envelope for more reliable addresses
        envelope = raw.get("envelope")
        if envelope:
            if isinstance(envelope, str):
                envelope = json.loads(envelope)
            if envelope.get("to"):
                to_addresses = envelope["to"]

        headers_raw = raw.get("headers", "")
        headers = {}
        if headers_raw:
            for line in headers_raw.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

        return {
            "from_email": from_email,
            "from_name": from_name,
            "to_addresses": to_addresses,
            "subject": raw.get("subject", ""),
            "body_text": raw.get("text", ""),
            "body_html": raw.get("html"),
            "message_id_header": headers.get("message-id"),
            "in_reply_to": headers.get("in-reply-to"),
            "references": headers.get("references"),
            "attachments": [],
            "timestamp": None,
        }

    def _parse_cloudflare_worker(self, raw: dict) -> Optional[dict]:
        """Parse Cloudflare Email Worker pre-parsed JSON payload."""
        from_field = raw.get("from", "")
        from_email = from_field
        from_name = None
        if "<" in from_field:
            from_name = from_field.split("<")[0].strip().strip('"')
            from_email = from_field.split("<")[1].rstrip(">")

        to_field = raw.get("to", "")
        to_addresses = [a.strip() for a in to_field.split(",") if a.strip()]

        return {
            "from_email": from_email,
            "from_name": from_name,
            "to_addresses": to_addresses,
            "subject": raw.get("subject", ""),
            "body_text": raw.get("text", ""),
            "body_html": raw.get("html"),
            "message_id_header": raw.get("messageId"),
            "in_reply_to": raw.get("inReplyTo"),
            "references": raw.get("references"),
            "attachments": raw.get("attachments", []),
            "timestamp": None,
        }

    def _resolve_inbound_address(self, instance_id: int, to_address: str) -> Optional[EmailInboundAddress]:
        """Find matching EmailInboundAddress for a to-address."""
        # Strip angle brackets and lowercase
        clean = to_address.strip().lower().lstrip("<").rstrip(">")
        # Try extracting just email from "Name <email>" format
        if "<" in to_address:
            clean = to_address.split("<")[1].rstrip(">").strip().lower()

        return self.db.query(EmailInboundAddress).filter(
            EmailInboundAddress.instance_id == instance_id,
            EmailInboundAddress.address == clean,
            EmailInboundAddress.is_active == True,
        ).first()

    def _handle_bounce_dsn(self, project_id: int, parsed: dict, raw_payload: dict) -> dict:
        """Parse a MAILER-DAEMON bounce and record delivery feedback."""
        import re
        body = parsed.get("body_text", "") or parsed.get("body_html", "") or ""

        # Extract original recipient from DSN format
        recipient_match = re.search(r"Final-Recipient:\s*rfc822;\s*(\S+)", body)
        original_recipient = recipient_match.group(1) if recipient_match else None

        # Extract diagnostic code
        diag_match = re.search(r"Diagnostic-Code:\s*smtp;\s*(.+?)(?:\n|$)", body, re.DOTALL)
        diagnostic = diag_match.group(1).strip() if diag_match else None

        # Extract status code
        status_match = re.search(r"Status:\s*(\d+\.\d+\.\d+)", body)
        status_code = status_match.group(1) if status_match else None

        # Classify: 5.x.x = permanent, 4.x.x = temporary
        feedback_type = "hard_bounce"
        if status_code and status_code.startswith("4"):
            feedback_type = "soft_bounce"
        elif diagnostic:
            lower_diag = diagnostic.lower()
            if any(w in lower_diag for w in ("over quota", "storage", "try again", "temporarily")):
                feedback_type = "soft_bounce"

        if original_recipient:
            from app.services.channels.delivery_feedback_service import DeliveryFeedbackService
            svc = DeliveryFeedbackService(self.db)
            svc.record_feedback(
                project_id=project_id,
                channel="email",
                recipient=original_recipient,
                feedback_type=feedback_type,
                reason=diagnostic or "Bounce notification from MAILER-DAEMON",
                provider="smtp_dsn",
                provider_code=status_code,
                provider_detail=body[:2000] if body else None,
                raw_payload=raw_payload,
            )
            self.db.commit()
            logger.info("DSN bounce processed: %s → %s for %s", feedback_type, original_recipient, project_id)
            return {"success": True, "type": "bounce_dsn", "recipient": original_recipient, "feedback_type": feedback_type}

        logger.warning("Could not parse bounce recipient from MAILER-DAEMON message (project %s)", project_id)
        return {"success": False, "error": "bounce_parse_failed"}

    def _extract_reply_tracking_id(self, address: str) -> Optional[str]:
        """Extract conversation ID from reply+{id}@domain pattern."""
        match = REPLY_TRACKING_PATTERN.search(address)
        return match.group(1) if match else None

    def verify_hmac(self, instance_id: int, payload_bytes: bytes, signature: str) -> bool:
        """Verify HMAC signature for webhook security."""
        config = self.db.query(CustomerSMTPConfig).filter(
            CustomerSMTPConfig.id == instance_id,
        ).first()
        if not config or not config.inbound_webhook_secret:
            return False

        import os
        from cryptography.fernet import Fernet
        enc_key = os.getenv("ENCRYPTION_KEY")
        secret = config.inbound_webhook_secret
        if enc_key:
            try:
                f = Fernet(enc_key.encode())
                secret = f.decrypt(secret.encode()).decode()
            except Exception:
                pass

        expected = hmac.new(
            secret.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
