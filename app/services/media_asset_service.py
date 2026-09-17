"""
Media Asset Service

CRUD operations, prompt catalog building, and response post-processing
for shareable media assets used by chatbots and agent teams.
"""

import os
import re
import uuid
import logging
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.models.media_assets import ProjectMediaAsset

logger = logging.getLogger(__name__)

# Regex to match [ASSET:slug] tokens in LLM responses
ASSET_TOKEN_RE = re.compile(r'\[ASSET:([\w\-]+)\]')

MEDIA_ASSETS_DIR = os.getenv("MEDIA_ASSETS_DIR", "/app/data/media_assets")


class MediaAssetService:
    """Service for managing project media assets."""

    def __init__(self, db: Session):
        self.db = db

    # ── CRUD ────────────────────────────────────────────────────────────

    def list_assets(
        self,
        project_id: int,
        chatbot_id: Optional[int] = None,
        specialist_id: Optional[int] = None,
        active_only: bool = False,
    ) -> List[ProjectMediaAsset]:
        """List assets for a project, optionally filtered by scope."""
        q = self.db.query(ProjectMediaAsset).filter(
            ProjectMediaAsset.project_id == project_id,
        )

        if chatbot_id is not None:
            # Assets scoped to this chatbot OR global (no scope)
            q = q.filter(
                or_(
                    ProjectMediaAsset.chatbot_id == chatbot_id,
                    ProjectMediaAsset.chatbot_id.is_(None),
                )
            )
        if specialist_id is not None:
            q = q.filter(
                or_(
                    ProjectMediaAsset.specialist_id == specialist_id,
                    ProjectMediaAsset.specialist_id.is_(None),
                )
            )
        if active_only:
            q = q.filter(ProjectMediaAsset.is_active == True)

        return q.order_by(ProjectMediaAsset.created_at.desc()).all()

    def get_asset(self, project_id: int, asset_id: int) -> Optional[ProjectMediaAsset]:
        return self.db.query(ProjectMediaAsset).filter(
            ProjectMediaAsset.id == asset_id,
            ProjectMediaAsset.project_id == project_id,
        ).first()

    def get_asset_by_slug(self, project_id: int, slug: str) -> Optional[ProjectMediaAsset]:
        return self.db.query(ProjectMediaAsset).filter(
            ProjectMediaAsset.project_id == project_id,
            ProjectMediaAsset.slug == slug,
        ).first()

    def create_asset(self, project_id: int, **kwargs) -> ProjectMediaAsset:
        asset = ProjectMediaAsset(project_id=project_id, **kwargs)
        self.db.add(asset)
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def update_asset(self, asset: ProjectMediaAsset, **kwargs) -> ProjectMediaAsset:
        for key, value in kwargs.items():
            if value is not None:
                setattr(asset, key, value)
        self.db.commit()
        self.db.refresh(asset)
        return asset

    def delete_asset(self, asset: ProjectMediaAsset) -> None:
        # Remove uploaded file if present
        if asset.file_path:
            try:
                file_full = Path(asset.file_path)
                if file_full.exists():
                    file_full.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete file {asset.file_path}: {e}")
        self.db.delete(asset)
        self.db.commit()

    # ── File Upload ─────────────────────────────────────────────────────

    def save_uploaded_file(
        self, project_id: int, file_content: bytes, filename: str,
    ) -> Tuple[str, str]:
        """
        Save an uploaded file to disk.

        Returns:
            (file_path, public_url)
        """
        ext = Path(filename).suffix or ""
        unique_name = f"{uuid.uuid4().hex}{ext}"

        directory = Path(MEDIA_ASSETS_DIR) / str(project_id)
        directory.mkdir(parents=True, exist_ok=True)

        file_path = directory / unique_name
        file_path.write_bytes(file_content)

        public_url = f"/api/v1/media-assets/files/{project_id}/{unique_name}"

        return str(file_path), public_url

    # ── Prompt Catalog Builder ──────────────────────────────────────────

    def build_asset_catalog(
        self,
        project_id: int,
        chatbot_id: Optional[int] = None,
        specialist_id: Optional[int] = None,
    ) -> str:
        """
        Build a text catalog of available media assets for injection
        into the LLM system prompt. Includes Knowledge Library assets
        with usage_mode 'direct' or 'both'.
        """
        assets = self.list_assets(
            project_id=project_id,
            chatbot_id=chatbot_id,
            specialist_id=specialist_id,
            active_only=True,
        )

        # Also include Knowledge Library assets with direct/both mode
        from app.models import KnowledgeAsset
        ka_assets = self.db.query(KnowledgeAsset).filter(
            KnowledgeAsset.project_id == project_id,
            KnowledgeAsset.usage_mode.in_(['direct', 'both']),
            KnowledgeAsset.slug.isnot(None),
            KnowledgeAsset.status == 'active',
        ).all()

        if not assets and not ka_assets:
            return ""

        file_assets = [a for a in assets if a.media_type != "link"]
        link_assets = [a for a in assets if a.media_type == "link"]

        parts = [
            "## Available Media Assets",
            "You can share files and links with the user by including [ASSET:slug] in your response.",
            "IMPORTANT: Always place [ASSET:slug] tokens ALONE on a separate line at the END of your message.",
            "Write your explanation first, then say something like 'Here it is:' and put the token on the next line.",
            "Only share assets when directly relevant to the conversation.",
        ]

        if file_assets:
            parts.append("")
            parts.append("Files you can share:")
            for a in file_assets:
                desc = f' {a.description}' if a.description else ''
                parts.append(
                    f'- [ASSET:{a.slug}] — "{a.label}" ({a.media_type}).{desc}'
                )

        if link_assets:
            parts.append("")
            parts.append("Links you can reference (include URL in your text):")
            for a in link_assets:
                desc = f' — {a.description}' if a.description else ''
                parts.append(
                    f'- "{a.label}": {a.media_url}{desc}'
                )

        if ka_assets:
            parts.append("")
            parts.append("Knowledge assets you can share:")
            for ka in ka_assets:
                sd = ka.source_data or {}
                url = sd.get('web_url') or sd.get('media_url') or sd.get('file_url') or ''
                if url.startswith('/'):
                    _pub = os.getenv('PUBLIC_BASE_URL') or os.getenv('BACKEND_BASE_URL', '')
                    url = f"{_pub}{url}"
                desc = f' — {ka.description}' if ka.description else ''
                parts.append(
                    f'- [ASSET:{ka.slug}] — "{ka.name}" ({ka.asset_type}). URL: {url}{desc}'
                )

        return "\n".join(parts)

    # ── Response Post-Processor ─────────────────────────────────────────

    def _is_direct_media_url(self, asset: ProjectMediaAsset) -> bool:
        """Check if an asset URL points to a directly sendable media file
        (as opposed to a web page like YouTube, docs site, etc.)."""
        if asset.file_path:
            return True
        if not asset.media_url:
            return False
        lower = asset.media_url.lower().split("?")[0]
        media_exts = (
            ".jpg", ".jpeg", ".png", ".gif", ".webp",
            ".mp4", ".avi", ".mov", ".webm",
            ".mp3", ".ogg", ".wav", ".aac",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        )
        return any(lower.endswith(ext) for ext in media_exts)

    def extract_asset_references(
        self, response_text: str, project_id: int,
    ) -> Tuple[str, List[Dict]]:
        """
        Extract [ASSET:slug] tokens from LLM response text.

        All resolved assets are stripped from the text and returned as items
        to be sent as follow-up messages:
        - Uploaded files / direct media URLs → sent via send_media_message()
        - External URLs (YouTube, web pages) → sent as a plain text message

        Returns:
            (cleaned_text, list_of_asset_dicts)
            Each dict has: url, type, caption, filename, send_as ("media" | "text")
        """
        slugs = ASSET_TOKEN_RE.findall(response_text)
        if not slugs:
            return response_text, []

        # Resolve all slugs (ProjectMediaAsset first, then KnowledgeAsset fallback)
        asset_map: Dict[str, Optional[ProjectMediaAsset]] = {}
        ka_map: Dict[str, any] = {}
        for slug in set(slugs):
            asset = self.get_asset_by_slug(project_id, slug)
            if asset and asset.is_active:
                asset_map[slug] = asset
            else:
                # Fallback to KnowledgeAsset with matching slug
                from app.models import KnowledgeAsset
                ka = self.db.query(KnowledgeAsset).filter(
                    KnowledgeAsset.project_id == project_id,
                    KnowledgeAsset.slug == slug,
                    KnowledgeAsset.status == 'active',
                    KnowledgeAsset.usage_mode.in_(['direct', 'both']),
                ).first()
                if ka:
                    ka_map[slug] = ka
                    asset_map[slug] = None  # Mark as resolved via ka_map
                else:
                    asset_map[slug] = None
                    logger.warning(f"Asset slug '{slug}' not found or inactive for project {project_id}")

        resolved_items: List[Dict] = []
        seen_slugs: set = set()

        def _replace_token(match: re.Match) -> str:
            slug = match.group(1)
            asset = asset_map.get(slug)
            ka = ka_map.get(slug)
            if not asset and not ka:
                return ''
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                if asset:
                    url = self.resolve_asset_url(asset)
                    if url:
                        resolved_items.append({
                            "url": url,
                            "type": asset.media_type,
                            "caption": asset.label,
                            "filename": asset.file_name,
                            "send_as": "media" if self._is_direct_media_url(asset) else "text",
                        })
                elif ka:
                    sd = ka.source_data or {}
                    url = sd.get('web_url') or sd.get('media_url') or sd.get('file_url')
                    if url and url.startswith('/'):
                        _pub = os.getenv('PUBLIC_BASE_URL') or os.getenv('BACKEND_BASE_URL', '')
                        url = f"{_pub}{url}"
                    if url:
                        resolved_items.append({
                            "url": url,
                            "type": ka.asset_type,
                            "caption": ka.name,
                            "filename": None,
                            "send_as": "text",
                        })
            return ''

        cleaned = ASSET_TOKEN_RE.sub(_replace_token, response_text)
        cleaned = re.sub(r'  +', ' ', cleaned)
        # Clean up blank lines left behind
        cleaned = re.sub(r'\n\s*\n', '\n', cleaned).strip()

        return cleaned, resolved_items

    def resolve_asset_url(self, asset: ProjectMediaAsset) -> Optional[str]:
        """Resolve the sendable URL for an asset (relative → absolute at send-time)."""
        if asset.media_url:
            url = asset.media_url
        elif asset.file_path:
            filename = Path(asset.file_path).name
            url = f"/api/v1/media-assets/files/{asset.project_id}/{filename}"
        else:
            return None
        if url.startswith("/"):
            public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_BASE_URL", "")
            return f"{public_base}{url}"
        return url
