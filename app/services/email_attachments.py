"""
Shared helpers for loading email attachment bytes.

Attachments are passed around as dicts: {"url": str, "filename": str}.
URLs may be absolute (downloaded via HTTP), or relative API paths — media-asset
files are read straight from local disk, anything else is resolved against
API_BASE_URL.
"""
import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_MEDIA_ASSET_URL_RE = re.compile(r"^/api/v1/media-assets/files/(\d+)/([^/?]+)$")

# Per-file safety cap so a huge attachment can't blow up SMTP payloads.
MAX_ATTACHMENT_BYTES = int(os.getenv("EMAIL_ATTACHMENT_MAX_BYTES", str(15 * 1024 * 1024)))


def load_attachment_bytes(att: dict) -> Optional[Tuple[bytes, str]]:
    """Resolve an attachment dict to (content_bytes, filename), or None on failure."""
    url = (att.get("url") or "").strip()
    if not url:
        return None
    filename = att.get("filename") or url.rsplit("/", 1)[-1].split("?")[0] or "attachment"

    try:
        if url.startswith("/"):
            match = _MEDIA_ASSET_URL_RE.match(url)
            if match:
                from app.services.media_asset_service import MEDIA_ASSETS_DIR
                file_path = Path(MEDIA_ASSETS_DIR) / match.group(1) / match.group(2)
                if file_path.exists():
                    content = file_path.read_bytes()
                    if len(content) > MAX_ATTACHMENT_BYTES:
                        logger.warning(f"Attachment {filename} exceeds size cap; skipping")
                        return None
                    return content, filename
            base = (os.getenv("API_BASE_URL") or "").rstrip("/")
            if not base:
                logger.warning(f"Cannot resolve relative attachment URL {url}: API_BASE_URL not set")
                return None
            url = f"{base}{url}"

        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        if len(resp.content) > MAX_ATTACHMENT_BYTES:
            logger.warning(f"Attachment {filename} exceeds size cap; skipping")
            return None
        return resp.content, filename
    except Exception as e:
        logger.warning(f"Failed to load attachment {filename} from {url}: {e}")
        return None
