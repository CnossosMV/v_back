"""
Media Storage Service — download and persist media files from WhatsApp.

Supports both Evolution API (getBase64 endpoint) and Meta Cloud API (media ID lookup).
Stores to /app/media/{project_id}/{date}/{uuid}.{ext} by default.
"""

import base64
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/app/media")


class MediaStorageService:
    """Download media from provider APIs and store locally."""

    def __init__(self, db=None):
        self.db = db

    async def download_and_store(
        self,
        media_url_or_id: str,
        provider_type: str,
        project_id: int,
        instance=None,
        mime_type: Optional[str] = None,
        raw_payload: Optional[Dict[str, Any]] = None,
        instance_name: Optional[str] = None,
    ) -> dict:
        """
        Download a media file and store it locally.

        Returns:
            {"local_path": str, "mime_type": str, "size_bytes": int}
        """
        try:
            if provider_type == "meta_cloud_api":
                data, mime_type = await self._download_meta(media_url_or_id, instance)
            elif provider_type == "evolution_api" and raw_payload:
                # Use Evolution's getBase64 endpoint to get decrypted media
                inst_name = instance_name or (instance.instance_name if instance else None)
                data, detected_mime = await self._download_evolution(raw_payload, inst_name)
                if detected_mime and detected_mime != "application/octet-stream":
                    mime_type = detected_mime
                if not data:
                    # Fallback to direct URL download
                    data, mime_type = await self._download_url(media_url_or_id, mime_type)
            else:
                # Direct URL download
                data, mime_type = await self._download_url(media_url_or_id, mime_type)

            if not data:
                return {"local_path": None, "mime_type": mime_type, "size_bytes": 0}

            # Last-resort MIME detection for octet-stream via magic bytes
            if not mime_type or mime_type == "application/octet-stream":
                mime_type = self._sniff_mime(data) or mime_type or "application/octet-stream"

            # Determine extension
            ext = self._ext_from_mime(mime_type or "application/octet-stream")
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            file_name = f"{uuid.uuid4().hex}.{ext}"
            rel_path = f"{project_id}/{date_str}/{file_name}"
            abs_path = os.path.join(MEDIA_ROOT, rel_path)

            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(data)

            logger.info(f"Stored media: {abs_path} ({len(data)} bytes, mime={mime_type})")
            return {
                "local_path": abs_path,
                "mime_type": mime_type,
                "size_bytes": len(data),
            }

        except Exception as e:
            logger.error(f"Error downloading media: {e}", exc_info=True)
            return {"local_path": None, "mime_type": mime_type, "size_bytes": 0}

    async def _download_evolution(
        self, raw_payload: Dict[str, Any], instance_name: Optional[str]
    ) -> tuple:
        """
        Download decrypted media from Evolution API's getBase64FromMediaMessage endpoint.

        Evolution API stores the decryption keys and fetches + decrypts the media.
        The raw WhatsApp CDN URLs are encrypted and cannot be downloaded directly.
        """
        if not instance_name:
            logger.warning("Cannot download Evolution media: no instance_name")
            return None, None

        evo_url = os.getenv("EVOLUTION_API_BASE_URL", os.getenv("EVOLUTION_API_URL", ""))
        evo_key = os.getenv("EVOLUTION_API_GLOBAL_KEY", os.getenv("EVOLUTION_API_KEY", ""))
        if not evo_url:
            logger.warning("Cannot download Evolution media: EVOLUTION_API_BASE_URL not set")
            return None, None

        # Build the request body from the raw webhook payload
        key = raw_payload.get("key", {})
        if not key.get("id"):
            logger.warning("Cannot download Evolution media: no message key in raw_payload")
            return None, None

        body = {
            "message": {
                "key": {
                    "id": key["id"],
                    "remoteJid": key.get("remoteJid", ""),
                    "fromMe": key.get("fromMe", False),
                },
            },
            "convertToMp4": False,
        }

        try:
            url = f"{evo_url.rstrip('/')}/chat/getBase64FromMediaMessage/{instance_name}"
            headers = {"apikey": evo_key}

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                result = resp.json()

            b64_data = result.get("base64")
            if not b64_data:
                logger.warning(f"Evolution getBase64 returned no base64 data: {result}")
                return None, None

            # Strip data URI prefix if present (e.g. "data:audio/ogg;base64,...")
            if "," in b64_data and b64_data.startswith("data:"):
                header, b64_data = b64_data.split(",", 1)
                # Extract MIME from "data:audio/ogg;base64" header
                detected_mime = header.replace("data:", "").split(";")[0]
            else:
                detected_mime = result.get("mimetype")

            data = base64.b64decode(b64_data)
            logger.info(f"Evolution getBase64 success: {len(data)} bytes, mime={detected_mime}")
            return data, detected_mime

        except Exception as e:
            logger.warning(f"Evolution getBase64 failed (will try direct URL): {e}")
            return None, None

    async def _download_url(self, url: str, mime_hint: Optional[str] = None) -> tuple:
        """Download from a direct URL (fallback)."""
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            ct_base = ct.split(";")[0].strip()
            # Prefer the hint when the server returns a generic octet-stream or nothing
            if (not ct_base or ct_base == "application/octet-stream") and mime_hint:
                return resp.content, mime_hint.split(";")[0].strip()
            return resp.content, ct_base or "application/octet-stream"

    async def _download_meta(self, media_id: str, instance) -> tuple:
        """Download from Meta Cloud API (two-step: get URL, then download)."""
        if not instance or not instance.meta_access_token_enc:
            logger.warning("Cannot download Meta media: no access token")
            return None, None

        from cryptography.fernet import Fernet

        key = os.getenv("ENCRYPTION_KEY", "")
        access_token = Fernet(key.encode()).decrypt(instance.meta_access_token_enc.encode()).decode()

        async with httpx.AsyncClient(timeout=60) as client:
            # Step 1: Get media URL
            meta_resp = await client.get(
                f"https://graph.facebook.com/v21.0/{media_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            meta_resp.raise_for_status()
            media_url = meta_resp.json().get("url")
            mime_type = meta_resp.json().get("mime_type", "application/octet-stream")

            if not media_url:
                return None, mime_type

            # Step 2: Download the actual media
            dl_resp = await client.get(
                media_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            dl_resp.raise_for_status()
            return dl_resp.content, mime_type

    @staticmethod
    def _sniff_mime(data: bytes) -> Optional[str]:
        """Detect MIME type from file magic bytes."""
        if len(data) < 4:
            return None
        # OGG container (WhatsApp voice messages)
        if data[:4] == b'OggS':
            return "audio/ogg"
        # MP3
        if data[:3] == b'ID3' or data[:2] == b'\xff\xfb':
            return "audio/mpeg"
        # RIFF (WAV, WebP, AVI)
        if data[:4] == b'RIFF' and len(data) >= 12:
            fmt = data[8:12]
            if fmt == b'WAVE':
                return "audio/wav"
            if fmt == b'WEBP':
                return "image/webp"
            if fmt == b'AVI ':
                return "video/avi"
        # MP4/M4A
        if len(data) >= 8 and data[4:8] == b'ftyp':
            return "video/mp4"
        # PNG
        if data[:4] == b'\x89PNG':
            return "image/png"
        # JPEG
        if data[:2] == b'\xff\xd8':
            return "image/jpeg"
        # PDF
        if data[:5] == b'%PDF-':
            return "application/pdf"
        return None

    @staticmethod
    def _ext_from_mime(mime: str) -> str:
        """Derive file extension from MIME type."""
        # Strip codec parameters (e.g. "audio/ogg; codecs=opus" → "audio/ogg")
        mime_base = mime.split(";")[0].strip()
        mapping = {
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
            "audio/wav": "wav",
            "audio/aac": "aac",
            "audio/webm": "webm",
            "audio/opus": "ogg",
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
            "video/mp4": "mp4",
            "video/3gpp": "3gp",
            "video/webm": "webm",
            "application/pdf": "pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "text/plain": "txt",
        }
        return mapping.get(mime_base, mime_base.split("/")[-1] if "/" in mime_base else "bin")
