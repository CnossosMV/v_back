"""
NoCode Mapping Service — CRUD, publish, rollback, validation for event mappings.
"""
import hashlib
import json
import re
from datetime import datetime
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, func as sa_func
from app.models import NoCodeMapping, NoCodeConfigSnapshot, ExtensionAuditLog


# Dangerous patterns in page patterns
_CATASTROPHIC_RE = re.compile(r"(\.\*){3,}|(\(.*\+\)){2,}")
_PII_PATTERNS = re.compile(
    r"(email|password|ssn|social.?security|credit.?card|card.?number|cvv|"
    r"phone.?number|date.?of.?birth|passport|driver.?license)",
    re.IGNORECASE,
)
_ALLOWED_VEF_STRATEGIES = {
    "data_attribute", "semantic_stable", "structural_landmark",
    "text_content", "visual_position",
}


class NoCodeMappingService:

    def __init__(self, db: Session):
        self.db = db

    # ── CRUD ──────────────────────────────────────────────────────────

    def upsert_mappings(
        self, project_id: int, mappings: List[Dict[str, Any]], user_id: int,
    ) -> List[NoCodeMapping]:
        """Upsert mappings by mapping_uid. Validates VEF, properties, page_pattern."""
        results = []
        for m in mappings:
            self._validate_mapping(m)

            existing = self.db.query(NoCodeMapping).filter(
                NoCodeMapping.project_id == project_id,
                NoCodeMapping.mapping_uid == m["mapping_uid"],
            ).first()

            props = None
            if m.get("properties"):
                props = [p.dict(by_alias=True) if hasattr(p, "dict") else p for p in m["properties"]]

            vef_data = m["vef"].dict(by_alias=True) if hasattr(m["vef"], "dict") else m["vef"]

            if existing:
                existing.event_name = m["event_name"]
                existing.trigger = m["trigger"]
                existing.vef = vef_data
                existing.page_pattern = m.get("page_pattern")
                existing.page_scope_type = m.get("page_scope_type", "glob")
                existing.viewport_scope = m.get("viewport_scope", "all")
                existing.properties = props
                existing.display_name = m.get("display_name")
                existing.consent_required = m.get("consent_required", False)
                existing.updated_by_id = user_id
                existing.version = existing.version + 1
                existing.status = "draft"
                results.append(existing)
            else:
                mapping = NoCodeMapping(
                    project_id=project_id,
                    mapping_uid=m["mapping_uid"],
                    event_name=m["event_name"],
                    trigger=m["trigger"],
                    vef=vef_data,
                    page_pattern=m.get("page_pattern"),
                    page_scope_type=m.get("page_scope_type", "glob"),
                    viewport_scope=m.get("viewport_scope", "all"),
                    properties=props,
                    display_name=m.get("display_name"),
                    consent_required=m.get("consent_required", False),
                    created_by_id=user_id,
                    updated_by_id=user_id,
                )
                self.db.add(mapping)
                results.append(mapping)

        self._audit(project_id, user_id, "mappings.draft_saved", "mapping",
                    str(len(mappings)))

        self.db.commit()
        for r in results:
            self.db.refresh(r)
        return results

    def get_mappings(
        self, project_id: int, status: Optional[str] = None,
    ) -> List[NoCodeMapping]:
        """Get all mappings, optionally filtered by status."""
        q = self.db.query(NoCodeMapping).filter(
            NoCodeMapping.project_id == project_id,
            NoCodeMapping.status != "archived",
        )
        if status:
            q = q.filter(NoCodeMapping.status == status)
        return q.order_by(NoCodeMapping.created_at.desc()).all()

    def delete_mapping(
        self, project_id: int, mapping_uid: str, user_id: int,
    ) -> bool:
        """Archive a mapping (soft delete)."""
        mapping = self.db.query(NoCodeMapping).filter(
            NoCodeMapping.project_id == project_id,
            NoCodeMapping.mapping_uid == mapping_uid,
        ).first()
        if not mapping:
            return False
        mapping.status = "archived"
        mapping.updated_by_id = user_id
        self._audit(project_id, user_id, "mappings.deleted", "mapping", mapping_uid)
        self.db.commit()
        return True

    # ── Publish ───────────────────────────────────────────────────────

    def publish(self, project_id: int, user_id: int) -> NoCodeConfigSnapshot:
        """Create immutable snapshot, set drafts to published."""
        drafts = self.db.query(NoCodeMapping).filter(
            NoCodeMapping.project_id == project_id,
            NoCodeMapping.status.in_(["draft", "published"]),
        ).all()

        if not drafts:
            raise ValueError("No mappings to publish")

        # Build snapshot payload
        snapshot_data = []
        for m in drafts:
            snapshot_data.append({
                "mappingUid": m.mapping_uid,
                "eventName": m.event_name,
                "trigger": m.trigger,
                "vef": m.vef,
                "pagePattern": m.page_pattern,
                "pageScopeType": m.page_scope_type,
                "viewportScope": m.viewport_scope,
                "properties": m.properties,
                "consentRequired": m.consent_required,
            })

        # Compute checksum
        canonical = json.dumps(snapshot_data, sort_keys=True, default=str)
        checksum = hashlib.sha256(canonical.encode()).hexdigest()

        # Config payload size check (512KB)
        if len(canonical) > 512 * 1024:
            raise ValueError("Config payload exceeds 512KB limit")

        # Get next version
        latest = self.db.query(sa_func.max(NoCodeConfigSnapshot.version)).filter(
            NoCodeConfigSnapshot.project_id == project_id,
        ).scalar() or 0

        snapshot = NoCodeConfigSnapshot(
            project_id=project_id,
            version=latest + 1,
            mappings_snapshot=snapshot_data,
            checksum=checksum,
            mapping_count=len(snapshot_data),
            published_by_id=user_id,
        )
        self.db.add(snapshot)

        # Mark all as published
        for m in drafts:
            m.status = "published"
            m.updated_by_id = user_id

        self._audit(project_id, user_id, "mappings.published", "config_snapshot",
                    str(latest + 1), {"count": len(snapshot_data), "checksum": checksum})

        self.db.commit()
        self.db.refresh(snapshot)
        return snapshot

    def get_published_mappings(self, project_id: int) -> List[Dict[str, Any]]:
        """Get latest snapshot's mappings for SDK config."""
        snapshot = self.db.query(NoCodeConfigSnapshot).filter(
            NoCodeConfigSnapshot.project_id == project_id,
        ).order_by(NoCodeConfigSnapshot.version.desc()).first()

        if not snapshot:
            return []
        return snapshot.mappings_snapshot

    def get_versions(self, project_id: int, limit: int = 10) -> List[NoCodeConfigSnapshot]:
        """List recent snapshots."""
        return self.db.query(NoCodeConfigSnapshot).filter(
            NoCodeConfigSnapshot.project_id == project_id,
        ).order_by(NoCodeConfigSnapshot.version.desc()).limit(limit).all()

    def rollback(
        self, project_id: int, version_id: int, user_id: int,
    ) -> NoCodeConfigSnapshot:
        """Restore a previous snapshot as a new version."""
        target = self.db.query(NoCodeConfigSnapshot).filter(
            NoCodeConfigSnapshot.project_id == project_id,
            NoCodeConfigSnapshot.version == version_id,
        ).first()
        if not target:
            raise ValueError(f"Version {version_id} not found")

        latest_version = self.db.query(sa_func.max(NoCodeConfigSnapshot.version)).filter(
            NoCodeConfigSnapshot.project_id == project_id,
        ).scalar() or 0

        # Create new snapshot from the old one
        new_snapshot = NoCodeConfigSnapshot(
            project_id=project_id,
            version=latest_version + 1,
            mappings_snapshot=target.mappings_snapshot,
            checksum=target.checksum,
            mapping_count=target.mapping_count,
            published_by_id=user_id,
        )
        self.db.add(new_snapshot)

        # Rebuild individual mappings from snapshot
        # Archive all current
        self.db.query(NoCodeMapping).filter(
            NoCodeMapping.project_id == project_id,
            NoCodeMapping.status != "archived",
        ).update({"status": "archived", "updated_by_id": user_id})

        # Re-create from snapshot
        for m_data in target.mappings_snapshot:
            mapping = NoCodeMapping(
                project_id=project_id,
                mapping_uid=m_data["mappingUid"],
                event_name=m_data["eventName"],
                trigger=m_data["trigger"],
                vef=m_data["vef"],
                page_pattern=m_data.get("pagePattern"),
                page_scope_type=m_data.get("pageScopeType", "glob"),
                viewport_scope=m_data.get("viewportScope", "all"),
                properties=m_data.get("properties"),
                consent_required=m_data.get("consentRequired", False),
                status="published",
                created_by_id=user_id,
                updated_by_id=user_id,
            )
            self.db.add(mapping)

        self._audit(project_id, user_id, "mappings.rollback", "config_snapshot",
                    str(version_id), {"restored_to": latest_version + 1})

        self.db.commit()
        self.db.refresh(new_snapshot)
        return new_snapshot

    # ── Validation ────────────────────────────────────────────────────

    def _validate_mapping(self, m: Dict[str, Any]):
        """Validate VEF, properties, and page pattern."""
        vef = m.get("vef")
        if vef:
            self._validate_vef(vef)

        props = m.get("properties")
        if props:
            self._validate_properties(props)

        page_pattern = m.get("page_pattern")
        scope_type = m.get("page_scope_type", "glob")
        if page_pattern:
            self._validate_page_pattern(page_pattern, scope_type)

    def _validate_vef(self, vef):
        """Validate VEF descriptor."""
        layers = vef.layers if hasattr(vef, "layers") else vef.get("layers", [])
        if not layers or len(layers) > 5:
            raise ValueError("VEF must have 1-5 layers")
        for layer in layers:
            strategy = layer.strategy if hasattr(layer, "strategy") else layer.get("strategy")
            if str(strategy) not in _ALLOWED_VEF_STRATEGIES and strategy not in _ALLOWED_VEF_STRATEGIES:
                raise ValueError(f"Invalid VEF strategy: {strategy}")
            confidence = layer.confidence if hasattr(layer, "confidence") else layer.get("confidence", 0)
            if not (0.0 <= float(confidence) <= 1.0):
                raise ValueError("VEF confidence must be 0.0-1.0")

    def _validate_page_pattern(self, pattern: str, scope_type: str):
        """Validate page pattern for safety."""
        if len(pattern) > 500:
            raise ValueError("Page pattern exceeds 500 chars")
        if scope_type == "glob":
            # Convert glob to regex to test for catastrophic backtracking
            regex = pattern.replace("*", "[^/]*")
            if _CATASTROPHIC_RE.search(regex):
                raise ValueError("Page pattern may cause catastrophic backtracking")
            try:
                re.compile(regex)
            except re.error:
                raise ValueError("Invalid page pattern")

    def _validate_properties(self, props):
        """Validate property definitions."""
        for p in props:
            name = p.name if hasattr(p, "name") else p.get("name", "")
            classification = (
                p.data_classification if hasattr(p, "data_classification")
                else p.get("dataClassification", p.get("data_classification", ""))
            )
            consent = (
                p.consent_required if hasattr(p, "consent_required")
                else p.get("consentRequired", p.get("consent_required", False))
            )

            # Check PII pattern in property name
            if _PII_PATTERNS.search(name) and str(classification) != "pii":
                raise ValueError(
                    f"Property '{name}' matches PII pattern but is not classified as 'pii'"
                )
            # PII must require consent
            if str(classification) == "pii" and not consent:
                raise ValueError(
                    f"Property '{name}' classified as PII must have consentRequired=true"
                )

            # No eval patterns in selectors
            selector = p.selector if hasattr(p, "selector") else p.get("selector", "")
            if selector and any(x in str(selector).lower() for x in ["eval(", "function(", "javascript:"]):
                raise ValueError("Selector contains forbidden pattern")

    def _audit(
        self, project_id: int, user_id: int,
        action: str, target_type: str, target_id: str,
        details: dict = None,
    ):
        log = ExtensionAuditLog(
            project_id=project_id,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        self.db.add(log)
