"""
Meta WhatsApp Cloud API Service

Client for the official Meta Graph API v21.0 — mirrors evolution_api_service.py style.
"""

import httpx
import hmac
import hashlib
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class MetaCloudAPIService:
    GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

    # ── Outbound messaging ──────────────────────────────────────────────

    async def send_text_message(
        self,
        phone_number_id: str,
        access_token: str,
        to: str,
        text: str,
    ) -> Dict[str, Any]:
        """Send a free-form text message (only works inside 24h window)."""
        url = f"{self.GRAPH_API_BASE}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                data = resp.json()
            if resp.status_code in (200, 201):
                msg_id = data.get("messages", [{}])[0].get("id", "")
                return {"success": True, "message_id": msg_id, "message_data": data}
            logger.error(f"Meta send_text error {resp.status_code}: {data}")
            return {
                "success": False,
                "error": data.get("error", {}).get("message", str(data)),
            }
        except Exception as e:
            logger.error(f"Meta send_text exception: {e}")
            return {"success": False, "error": str(e)}

    async def send_template_message(
        self,
        phone_number_id: str,
        access_token: str,
        to: str,
        template_name: str,
        language_code: str = "en_US",
        components: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Send a pre-approved template message (works outside 24h window)."""
        url = f"{self.GRAPH_API_BASE}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        template_obj: Dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        if components:
            template_obj["components"] = components

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": template_obj,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                data = resp.json()
            if resp.status_code in (200, 201):
                msg_id = data.get("messages", [{}])[0].get("id", "")
                return {"success": True, "message_id": msg_id, "message_data": data}
            logger.error(f"Meta send_template error {resp.status_code}: {data}")
            return {
                "success": False,
                "error": data.get("error", {}).get("message", str(data)),
            }
        except Exception as e:
            logger.error(f"Meta send_template exception: {e}")
            return {"success": False, "error": str(e)}

    async def send_media_message(
        self,
        phone_number_id: str,
        access_token: str,
        to: str,
        media_url: str,
        media_type: str = "image",
        caption: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a media message via Meta Cloud API (only inside 24h window)."""
        url = f"{self.GRAPH_API_BASE}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        media_obj: Dict[str, Any] = {"link": media_url}
        if caption:
            media_obj["caption"] = caption
        if filename:
            media_obj["filename"] = filename

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": media_type,
            media_type: media_obj,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                data = resp.json()
            if resp.status_code in (200, 201):
                msg_id = data.get("messages", [{}])[0].get("id", "")
                return {"success": True, "message_id": msg_id, "message_data": data}
            logger.error(f"Meta send_media error {resp.status_code}: {data}")
            return {
                "success": False,
                "error": data.get("error", {}).get("message", str(data)),
            }
        except Exception as e:
            logger.error(f"Meta send_media exception: {e}")
            return {"success": False, "error": str(e)}

    # ── Template retrieval ────────────────────────────────────────────────

    async def get_template_by_name(
        self,
        waba_id: str,
        access_token: str,
        template_name: str,
        language_code: str = "en_US",
    ) -> Optional[Dict]:
        """Fetch a message template definition from Meta Graph API by name.

        Returns the first template matching the given language_code, or None.
        """
        url = f"{self.GRAPH_API_BASE}/{waba_id}/message_templates"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "name": template_name,
            "fields": "name,language,status,category,components",
            "limit": "10",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()
            if resp.status_code != 200:
                logger.error(f"Meta get_template error {resp.status_code}: {data}")
                return None
            templates = data.get("data", [])
            for tpl in templates:
                if tpl.get("language") == language_code:
                    return tpl
            # If exact language not found, return first match
            return templates[0] if templates else None
        except Exception as e:
            logger.error(f"Meta get_template_by_name exception: {e}")
            return None

    # ── Credential validation ───────────────────────────────────────────

    async def validate_credentials(
        self,
        phone_number_id: str,
        access_token: str,
    ) -> Dict[str, Any]:
        """Validate Meta credentials by fetching the phone number info."""
        url = f"{self.GRAPH_API_BASE}/{phone_number_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"fields": "display_phone_number,verified_name,quality_rating,id"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()
            if resp.status_code == 200:
                return {
                    "valid": True,
                    "phone_number": data.get("display_phone_number"),
                    "verified_name": data.get("verified_name"),
                    "quality_rating": data.get("quality_rating"),
                }
            return {
                "valid": False,
                "error": data.get("error", {}).get("message", str(data)),
            }
        except Exception as e:
            logger.error(f"Meta validate_credentials exception: {e}")
            return {"valid": False, "error": str(e)}

    # ── Webhook signature verification ──────────────────────────────────

    @staticmethod
    def verify_webhook_signature(
        payload_bytes: bytes,
        signature_header: str,
        app_secret: str,
    ) -> bool:
        """Verify X-Hub-Signature-256 HMAC-SHA256 signature."""
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected_sig = signature_header[len("sha256="):]
        computed = hmac.new(
            app_secret.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, expected_sig)


# Singleton instance
meta_cloud_api_service = MetaCloudAPIService()
