"""
EmailSender — unified dispatch for SMTP and generic API email instances.
"""
import logging
import os
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.models import EmailInstance
from app.services.email_attachments import load_attachment_bytes

logger = logging.getLogger(__name__)


def _decrypt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        return value
    try:
        f = Fernet(key.encode())
        return f.decrypt(value.encode()).decode()
    except Exception:
        return value


def _encrypt(value: str) -> str:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        return value
    f = Fernet(key.encode())
    return f.encrypt(value.encode()).decode()


_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _substitute(template: str, variables: dict) -> str:
    """Replace {{var}} placeholders in a string."""
    def _repl(m):
        return str(variables.get(m.group(1), m.group(0)))
    return _VAR_RE.sub(_repl, template)


def _substitute_obj(obj, variables: dict):
    """Recursively substitute {{var}} placeholders in dicts/lists/strings."""
    if isinstance(obj, str):
        return _substitute(obj, variables)
    if isinstance(obj, dict):
        return {k: _substitute_obj(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_obj(item, variables) for item in obj]
    return obj


def _extract_json_path(data: dict, path: str):
    """Extract value from nested dict via dot-separated path (e.g. 'data.id')."""
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


class EmailSender:
    def __init__(self, db: Session):
        self.db = db

    async def send_email(
        self,
        instance: EmailInstance,
        to_email: str,
        subject: str,
        html_body: str,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        body_format: Optional[str] = None,
        message_id: Optional[str] = None,
        attachments: Optional[list] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        if instance.provider_type == "smtp":
            return self._send_via_smtp(
                instance, to_email, subject, html_body,
                from_email=from_email, from_name=from_name,
                reply_to=reply_to, body_format=body_format,
                message_id=message_id, attachments=attachments,
                extra_headers=extra_headers,
            )
        elif instance.provider_type == "api":
            return await self._send_via_api(
                instance, to_email, subject, html_body,
                from_email=from_email, from_name=from_name,
                reply_to=reply_to, attachments=attachments,
                extra_headers=extra_headers, message_id=message_id,
            )
        return {"success": False, "message": f"Unknown provider: {instance.provider_type}"}

    async def verify_instance(self, instance: EmailInstance) -> dict:
        if instance.provider_type == "smtp":
            return self._verify_smtp(instance)
        elif instance.provider_type == "api":
            return await self._verify_api(instance)
        return {"success": False, "message": f"Unknown provider: {instance.provider_type}"}

    # ── SMTP ──────────────────────────────────────────────────────────────

    def _send_via_smtp(
        self,
        instance: EmailInstance,
        to_email: str,
        subject: str,
        html_body: str,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        body_format: Optional[str] = None,
        message_id: Optional[str] = None,
        attachments: Optional[list] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        submission_started = False
        try:
            sender_email = from_email or instance.from_email
            sender_name = from_name or instance.from_name
            password = _decrypt(instance.smtp_password_enc)

            alt_part = MIMEMultipart("alternative")

            # Attach plain text version
            if body_format == "plain" or not html_body.strip().startswith("<"):
                plain_text = html_body
                alt_part.attach(MIMEText(plain_text, "plain"))
            else:
                import re as _re
                plain_text = _re.sub(r"<[^>]+>", "", html_body)
                alt_part.attach(MIMEText(plain_text, "plain"))
                alt_part.attach(MIMEText(html_body, "html"))

            if attachments:
                from email.mime.base import MIMEBase
                from email import encoders
                msg = MIMEMultipart("mixed")
                msg.attach(alt_part)
                for att in attachments:
                    loaded = load_attachment_bytes(att)
                    if not loaded:
                        continue
                    att_bytes, att_filename = loaded
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(att_bytes)
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f'attachment; filename="{att_filename}"')
                    msg.attach(part)
            else:
                msg = alt_part

            msg["Subject"] = subject
            msg["To"] = to_email
            if sender_name:
                msg["From"] = f"{sender_name} <{sender_email}>"
            else:
                msg["From"] = sender_email
            if reply_to:
                msg["Reply-To"] = reply_to
            if message_id:
                msg["Message-ID"] = message_id
            for header_name, header_value in (extra_headers or {}).items():
                msg[header_name] = header_value

            # Connect and send
            if instance.smtp_use_ssl:
                server = smtplib.SMTP_SSL(instance.smtp_server, instance.smtp_port or 465)
            else:
                server = smtplib.SMTP(instance.smtp_server, instance.smtp_port or 587)

            if instance.smtp_use_tls and not instance.smtp_use_ssl:
                server.starttls()

            if instance.smtp_username and password:
                server.login(instance.smtp_username, password)

            submission_started = True
            server.sendmail(sender_email, [to_email], msg.as_string())
            try:
                server.quit()
            except Exception as exc:
                # SMTP accepted the DATA transaction already; a QUIT failure
                # must not turn a successful submission into a retry.
                logger.warning("SMTP quit failed after accepted message %s: %s", message_id, exc)

            return {
                "success": True,
                "message": "Email sent via SMTP instance",
                "provider_message_id": message_id,
            }
        except Exception as e:
            logger.error(f"SMTP send error (instance {instance.id}): {e}")
            ambiguous = submission_started and isinstance(
                e,
                (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionError, OSError),
            )
            return {
                "success": False,
                "message": str(e),
                "provider_message_id": message_id,
                "error_code": "submission_unknown" if ambiguous else "smtp_rejected",
            }

    def _verify_smtp(self, instance: EmailInstance) -> dict:
        try:
            password = _decrypt(instance.smtp_password_enc)
            if instance.smtp_use_ssl:
                server = smtplib.SMTP_SSL(instance.smtp_server, instance.smtp_port or 465)
            else:
                server = smtplib.SMTP(instance.smtp_server, instance.smtp_port or 587)

            if instance.smtp_use_tls and not instance.smtp_use_ssl:
                server.starttls()

            if instance.smtp_username and password:
                server.login(instance.smtp_username, password)

            server.ehlo()
            server.quit()
            return {"success": True, "message": "SMTP connection verified"}
        except Exception as e:
            logger.error(f"SMTP verify error (instance {instance.id}): {e}")
            return {"success": False, "message": str(e)}

    # ── Generic API ───────────────────────────────────────────────────────

    async def _send_via_api(
        self,
        instance: EmailInstance,
        to_email: str,
        subject: str,
        html_body: str,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        attachments: Optional[list] = None,
        extra_headers: Optional[dict] = None,
        message_id: Optional[str] = None,
    ) -> dict:
        submission_started = False
        try:
            # Generic API providers have no universal attachment contract —
            # deliver attachments as download links appended to the body.
            if attachments:
                base = (os.getenv("API_BASE_URL") or "").rstrip("/")
                links = []
                for att in attachments:
                    url = (att.get("url") or "").strip()
                    if not url:
                        continue
                    if url.startswith("/") and base:
                        url = f"{base}{url}"
                    name = att.get("filename") or url.rsplit("/", 1)[-1]
                    links.append(f'<li><a href="{url}">{name}</a></li>')
                if links:
                    html_body = f"{html_body}<hr><p>Attachments:</p><ul>{''.join(links)}</ul>"

            api_key = _decrypt(instance.api_key_enc)
            config = instance.api_config or {}

            endpoint_url = config.get("endpoint_url")
            if not endpoint_url:
                return {"success": False, "message": "No endpoint_url configured"}

            method = config.get("method", "POST").upper()
            auth_type = config.get("auth_type", "api_key_header")

            # Build variables for template substitution
            import re as _re
            text_body = _re.sub(r"<[^>]+>", "", html_body)
            variables = {
                "to_email": to_email,
                "subject": subject,
                "html_body": html_body,
                "text_body": text_body,
                "from_email": from_email or instance.from_email,
                "from_name": from_name or instance.from_name or "",
                "reply_to": reply_to or "",
                # Generic providers opt into RFC 8058 by referencing these
                # variables in their configured body/header templates.
                "extra_headers": extra_headers or {},
                "list_unsubscribe": (extra_headers or {}).get("List-Unsubscribe", ""),
                "list_unsubscribe_post": (extra_headers or {}).get("List-Unsubscribe-Post", ""),
                "message_id": message_id or "",
                "idempotency_key": message_id or "",
            }

            # Build request body from template
            body_template = config.get("body_template", {})
            body = _substitute_obj(body_template, variables)

            # Build headers
            headers = {"Content-Type": "application/json"}
            for k, v in config.get("extra_headers", {}).items():
                headers[k] = v

            # Auth
            if auth_type == "api_key_header" and api_key:
                header_name = config.get("auth_header_name", "X-API-Key")
                headers[header_name] = api_key
            elif auth_type == "bearer" and api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            elif auth_type == "basic" and api_key:
                import base64
                encoded = base64.b64encode(f"{api_key}:".encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
            elif auth_type == "query_param" and api_key:
                param_name = config.get("auth_param_name", "api_key")
                sep = "&" if "?" in endpoint_url else "?"
                endpoint_url = f"{endpoint_url}{sep}{param_name}={api_key}"

            # Send request
            submission_started = True
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(method, endpoint_url, json=body, headers=headers)

            # Check success
            success_check = config.get("success_check", {"type": "status_code", "expected": [200, 201, 202]})
            success = False
            if success_check.get("type") == "status_code":
                expected = success_check.get("expected", [200, 201, 202])
                success = response.status_code in expected
            elif success_check.get("type") == "json_path":
                try:
                    resp_json = response.json()
                    path = success_check.get("path", "")
                    actual = _extract_json_path(resp_json, path)
                    success = actual == success_check.get("expected_value")
                except Exception:
                    success = False

            # Extract message ID
            provider_message_id = None
            msg_id_path = config.get("message_id_path")
            if msg_id_path and success:
                try:
                    resp_json = response.json()
                    provider_message_id = _extract_json_path(resp_json, msg_id_path)
                except Exception:
                    pass

            if success:
                return {
                    "success": True,
                    "message": "Email sent via API instance",
                    "provider_message_id": provider_message_id or message_id,
                    "status_code": response.status_code,
                }
            else:
                return {
                    "success": False,
                    "message": f"API returned status {response.status_code}: {response.text[:500]}",
                    "status_code": response.status_code,
                }
        except Exception as e:
            logger.error(f"API send error (instance {instance.id}): {e}")
            return {
                "success": False,
                "message": str(e),
                "provider_message_id": message_id,
                "error_code": "submission_unknown" if submission_started else "api_transport_error",
            }

    async def _verify_api(self, instance: EmailInstance) -> dict:
        """Verify API instance by checking if the endpoint is reachable."""
        try:
            config = instance.api_config or {}
            endpoint_url = config.get("endpoint_url")
            if not endpoint_url:
                return {"success": False, "message": "No endpoint_url configured"}

            api_key = _decrypt(instance.api_key_enc)
            auth_type = config.get("auth_type", "api_key_header")

            headers = {}
            if auth_type == "api_key_header" and api_key:
                header_name = config.get("auth_header_name", "X-API-Key")
                headers[header_name] = api_key
            elif auth_type == "bearer" and api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            async with httpx.AsyncClient(timeout=15.0) as client:
                # Try a HEAD or GET to verify connectivity and auth
                response = await client.head(endpoint_url, headers=headers)

            if response.status_code < 500:
                return {"success": True, "message": f"API endpoint reachable (status {response.status_code})"}
            else:
                return {"success": False, "message": f"API returned server error: {response.status_code}"}
        except Exception as e:
            logger.error(f"API verify error (instance {instance.id}): {e}")
            return {"success": False, "message": str(e)}
