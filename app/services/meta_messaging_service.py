"""
Meta Messaging Service (Messenger + Instagram DMs & comments)

Graph API client for Facebook Page / Instagram messaging via the
app-level Meta app (Facebook Login for Business). Mirrors
meta_cloud_api_service.py style but is page-token based.

App-level credentials come from env: META_APP_ID, META_APP_SECRET,
META_CONFIG_ID, META_WEBHOOK_VERIFY_TOKEN.
"""

import os
import httpx
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v25.0"

# Graph error codes that mean the page token is dead (reconnect required)
TOKEN_INVALID_CODES = {102, 190}

# Webhook fields we subscribe pages to. `feed` delivers FB comments;
# comment events are gated per-connection by fb_comments_enabled.
PAGE_SUBSCRIBED_FIELDS = [
    "messages",
    "messaging_postbacks",
    "message_echoes",
    "message_reads",
    "feed",
]


def get_meta_app_config() -> Optional[Dict[str, str]]:
    """Return app-level Meta config from env, or None when not configured."""
    app_id = os.getenv("META_APP_ID")
    app_secret = os.getenv("META_APP_SECRET")
    config_id = os.getenv("META_CONFIG_ID")
    if not app_id or not app_secret:
        return None
    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "config_id": config_id or "",
        "verify_token": os.getenv("META_WEBHOOK_VERIFY_TOKEN", ""),
    }


class MetaMessagingService:
    """Client for Messenger/Instagram messaging + comments via Graph API."""

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _error_result(data: Dict[str, Any]) -> Dict[str, Any]:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        code = err.get("code")
        return {
            "success": False,
            "error": err.get("message", str(data)),
            "error_code": code,
            "error_subcode": err.get("error_subcode"),
            "token_invalid": code in TOKEN_INVALID_CODES,
        }

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{GRAPH_API_BASE}/{path}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
            if resp.status_code == 200:
                return {"success": True, "data": data}
            logger.error(f"Meta GET {path} error {resp.status_code}: {data}")
            return self._error_result(data)
        except Exception as e:
            logger.error(f"Meta GET {path} exception: {e}")
            return {"success": False, "error": str(e), "error_code": None, "token_invalid": False}

    async def _post(self, path: str, *, json: Optional[Dict] = None, params: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{GRAPH_API_BASE}/{path}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=json, params=params)
                data = resp.json()
            if resp.status_code in (200, 201):
                return {"success": True, "data": data}
            logger.error(f"Meta POST {path} error {resp.status_code}: {data}")
            return self._error_result(data)
        except Exception as e:
            logger.error(f"Meta POST {path} exception: {e}")
            return {"success": False, "error": str(e), "error_code": None, "token_invalid": False}

    # ── OAuth (Facebook Login for Business) ──────────────────────────────

    def build_oauth_dialog_url(self, redirect_uri: str, state: str) -> Optional[str]:
        cfg = get_meta_app_config()
        if not cfg:
            return None
        params = {
            "client_id": cfg["app_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
        }
        # config_id carries the permission bundle for FB Login for Business
        if cfg["config_id"]:
            params["config_id"] = cfg["config_id"]
        return f"https://www.facebook.com/v25.0/dialog/oauth?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """OAuth code -> short-lived user access token."""
        cfg = get_meta_app_config()
        if not cfg:
            return {"success": False, "error": "Meta app not configured (META_APP_ID/META_APP_SECRET missing)"}
        result = await self._get(
            "oauth/access_token",
            {
                "client_id": cfg["app_id"],
                "client_secret": cfg["app_secret"],
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        if result["success"]:
            return {"success": True, "access_token": result["data"].get("access_token")}
        return result

    async def exchange_long_lived(self, user_token: str) -> Dict[str, Any]:
        """Short-lived user token -> long-lived (~60 days) user token."""
        cfg = get_meta_app_config()
        if not cfg:
            return {"success": False, "error": "Meta app not configured"}
        result = await self._get(
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": cfg["app_id"],
                "client_secret": cfg["app_secret"],
                "fb_exchange_token": user_token,
            },
        )
        if result["success"]:
            return {"success": True, "access_token": result["data"].get("access_token")}
        return result

    async def list_pages(self, user_token: str) -> Dict[str, Any]:
        """List pages the user manages, with page tokens + linked IG accounts.

        Page access tokens obtained from a long-lived user token do not expire.
        """
        pages: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {
            "access_token": user_token,
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "limit": 100,
        }
        path = "me/accounts"
        while True:
            result = await self._get(path, params)
            if not result["success"]:
                return result
            data = result["data"]
            for p in data.get("data", []):
                ig = p.get("instagram_business_account") or {}
                pages.append(
                    {
                        "page_id": p.get("id"),
                        "name": p.get("name"),
                        "access_token": p.get("access_token"),
                        "ig_account_id": ig.get("id"),
                        "ig_username": ig.get("username"),
                    }
                )
            next_url = (data.get("paging") or {}).get("next")
            if not next_url:
                break
            # paging.next is a fully-qualified URL; strip the Graph base
            path = next_url.replace(f"{GRAPH_API_BASE}/", "")
            params = {}
        return {"success": True, "pages": pages}

    # ── Page setup ────────────────────────────────────────────────────────

    async def subscribe_page(
        self, page_id: str, page_token: str, fields: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Subscribe the app to a page's webhook fields (starts webhook delivery)."""
        return await self._post(
            f"{page_id}/subscribed_apps",
            params={
                "access_token": page_token,
                "subscribed_fields": ",".join(fields or PAGE_SUBSCRIBED_FIELDS),
            },
        )

    async def unsubscribe_page(self, page_id: str, page_token: str) -> Dict[str, Any]:
        url = f"{GRAPH_API_BASE}/{page_id}/subscribed_apps"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.delete(url, params={"access_token": page_token})
                data = resp.json()
            if resp.status_code == 200:
                return {"success": True, "data": data}
            return self._error_result(data)
        except Exception as e:
            return {"success": False, "error": str(e), "error_code": None, "token_invalid": False}

    async def validate_page_token(self, page_id: str, page_token: str) -> Dict[str, Any]:
        """Validate a page token; also refreshes linked IG account info."""
        result = await self._get(
            page_id,
            {
                "access_token": page_token,
                "fields": "id,name,instagram_business_account{id,username}",
            },
        )
        if result["success"]:
            data = result["data"]
            ig = data.get("instagram_business_account") or {}
            return {
                "success": True,
                "page_name": data.get("name"),
                "ig_account_id": ig.get("id"),
                "ig_username": ig.get("username"),
            }
        return result

    # ── Messaging (Messenger PSID + Instagram IGSID share the endpoint) ──

    async def send_message(
        self,
        page_id: str,
        page_token: str,
        recipient_id: str,
        text: Optional[str] = None,
        attachment: Optional[Dict[str, Any]] = None,
        messaging_type: str = "RESPONSE",
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a DM to a PSID (Messenger) or IGSID (Instagram).

        attachment: {"type": "image|video|audio|file", "payload": {"url": ..., "is_reusable": true}}
        tag="HUMAN_AGENT" with messaging_type="MESSAGE_TAG" allows human replies
        up to 7 days after the last user message (requires app review feature).
        """
        message: Dict[str, Any] = {}
        if text:
            message["text"] = text
        if attachment:
            message["attachment"] = attachment
        if not message:
            return {"success": False, "error": "Empty message"}

        payload: Dict[str, Any] = {
            "recipient": {"id": recipient_id},
            "messaging_type": messaging_type,
            "message": message,
        }
        if tag:
            payload["tag"] = tag

        result = await self._post(
            f"{page_id}/messages", json=payload, params={"access_token": page_token}
        )
        if result["success"]:
            return {
                "success": True,
                "message_id": result["data"].get("message_id"),
                "recipient_id": result["data"].get("recipient_id"),
            }
        return result

    async def send_private_reply(
        self, page_id: str, page_token: str, comment_id: str, text: str
    ) -> Dict[str, Any]:
        """Private reply to a FB/IG comment (comment -> DM). One per comment, 7-day window."""
        payload = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": text},
        }
        result = await self._post(
            f"{page_id}/messages", json=payload, params={"access_token": page_token}
        )
        if result["success"]:
            return {
                "success": True,
                "message_id": result["data"].get("message_id"),
                "recipient_id": result["data"].get("recipient_id"),
            }
        return result

    # ── Comments (public replies) ────────────────────────────────────────

    async def reply_to_fb_comment(
        self, comment_id: str, page_token: str, text: str
    ) -> Dict[str, Any]:
        result = await self._post(
            f"{comment_id}/comments",
            params={"access_token": page_token, "message": text},
        )
        if result["success"]:
            return {"success": True, "comment_id": result["data"].get("id")}
        return result

    async def reply_to_ig_comment(
        self, ig_comment_id: str, page_token: str, text: str
    ) -> Dict[str, Any]:
        result = await self._post(
            f"{ig_comment_id}/replies",
            params={"access_token": page_token, "message": text},
        )
        if result["success"]:
            return {"success": True, "comment_id": result["data"].get("id")}
        return result

    # ── Profiles (best-effort display names) ─────────────────────────────

    async def get_messenger_profile(self, psid: str, page_token: str) -> Optional[Dict[str, Any]]:
        result = await self._get(
            psid, {"access_token": page_token, "fields": "first_name,last_name,profile_pic"}
        )
        if result["success"]:
            d = result["data"]
            name = " ".join(filter(None, [d.get("first_name"), d.get("last_name")])) or None
            return {"name": name, "profile_pic": d.get("profile_pic")}
        return None

    async def get_ig_profile(self, igsid: str, page_token: str) -> Optional[Dict[str, Any]]:
        result = await self._get(
            igsid, {"access_token": page_token, "fields": "name,username,profile_pic"}
        )
        if result["success"]:
            d = result["data"]
            return {
                "name": d.get("name") or d.get("username"),
                "username": d.get("username"),
                "profile_pic": d.get("profile_pic"),
            }
        return None


# Singleton instance
meta_messaging_service = MetaMessagingService()


def mark_connection_error(db, connection, result: Dict[str, Any]) -> None:
    """Flip a MetaPageConnection to error status when the token is invalid."""
    if not result.get("token_invalid"):
        return
    try:
        connection.status = "error"
        connection.last_error = result.get("error") or "Invalid page access token"
        db.commit()
        logger.warning(
            f"MetaPageConnection {connection.id} (page {connection.page_id}) marked error: "
            f"{connection.last_error}"
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to mark Meta connection error")
