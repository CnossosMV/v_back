"""
Import approved Meta WhatsApp Cloud API templates into MessagingTemplate rows.

Reuses the LIST endpoint exposed by meta_whatsapp router (Graph API v21.0).
Idempotent upsert keyed on (whatsapp_instance_id, meta_template_name, meta_language)
via the partial unique index `uq_tpl_meta_import`.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.models import WhatsAppInstance
from app.models.messaging import MessagingTemplate, ChannelType

logger = logging.getLogger(__name__)


META_GRAPH_BASE = "https://graph.facebook.com/v21.0"


def _decrypt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        return value
    try:
        return Fernet(key.encode()).decrypt(value.encode()).decode()
    except Exception:
        return None


def _instance_credentials(instance: WhatsAppInstance) -> tuple[Optional[str], Optional[str]]:
    token = _decrypt(instance.meta_access_token_enc) if instance.meta_access_token_enc else None
    waba = instance.meta_waba_id
    return token, waba


async def fetch_meta_templates(instance: WhatsAppInstance) -> List[Dict[str, Any]]:
    """Call Meta Graph API and return the raw template list for an instance."""
    access_token, waba_id = _instance_credentials(instance)
    if not access_token or not waba_id:
        raise ValueError("Meta credentials not configured on this instance")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{META_GRAPH_BASE}/{waba_id}/message_templates",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "name,language,status,category,components", "limit": 200},
        )
        data = resp.json()
        if resp.status_code != 200:
            err = data.get("error", {}).get("message", f"Graph API error {resp.status_code}")
            raise RuntimeError(err)
    return data.get("data", []) or []


def _slugify(name: str, language: str) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", f"{name}-{language}".lower()).strip("-")
    return base[:100] or "meta-template"


def _body_preview(components: List[Dict[str, Any]]) -> str:
    """Extract the BODY component's text as a human-readable body preview."""
    for comp in components or []:
        if comp.get("type", "").upper() == "BODY":
            text = comp.get("text")
            if isinstance(text, str):
                return text
    return ""


def import_templates(
    db: Session,
    instance: WhatsAppInstance,
    template_rows: List[Dict[str, Any]],
    *,
    selected_keys: Optional[List[tuple[str, str]]] = None,
    only_approved: bool = True,
    project_id: Optional[int] = None,
) -> Dict[str, int]:
    """Upsert a list of Meta template descriptors into MessagingTemplate.

    Each descriptor is the dict shape returned by Graph API:
      {name, language, status, category, components}.

    Returns counts: {"created": int, "updated": int, "skipped": int}.
    `selected_keys` is an optional allow-list of (name, language) tuples.
    """
    created = 0
    updated = 0
    skipped = 0
    now = datetime.utcnow()
    allow: Optional[set] = (
        {(n, l) for n, l in selected_keys} if selected_keys is not None else None
    )

    for t in template_rows:
        name = t.get("name")
        language = t.get("language") or ""
        status = (t.get("status") or "").upper()
        components = t.get("components") or []
        if not name:
            skipped += 1
            continue
        if allow is not None and (name, language) not in allow:
            skipped += 1
            continue
        if only_approved and status != "APPROVED":
            skipped += 1
            continue

        existing = (
            db.query(MessagingTemplate)
            .filter(
                MessagingTemplate.whatsapp_instance_id == instance.id,
                MessagingTemplate.meta_template_name == name,
                MessagingTemplate.meta_language == language,
                MessagingTemplate.external_source == "meta_cloud",
            )
            .first()
        )
        body = _body_preview(components) or f"[Meta template: {name}]"
        if existing:
            existing.meta_components = components
            existing.body = body
            existing.external_last_synced_at = now
            updated += 1
        else:
            tpl = MessagingTemplate(
                project_id=project_id if project_id is not None else instance.project_id,
                slug=_slugify(name, language),
                name=f"{name} ({language})" if language else name,
                channel_type=ChannelType.whatsapp,
                body=body,
                meta_template_name=name,
                meta_language=language,
                meta_components=components,
                whatsapp_instance_id=instance.id,
                external_source="meta_cloud",
                external_last_synced_at=now,
                is_active=True,
            )
            db.add(tpl)
            created += 1

    db.commit()
    logger.info(
        "Meta template import for instance %s: created=%d updated=%d skipped=%d",
        instance.id, created, updated, skipped,
    )
    return {"created": created, "updated": updated, "skipped": skipped}
