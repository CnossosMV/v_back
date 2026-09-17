"""Canonical CRUD and compatibility projection for project markets."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app import models
from app.services.project_foundation import normalize_market_config


MARKET_STATUSES = {"active", "archived"}
MARKET_MUTABLE_FIELDS = {
    "country_code",
    "region_codes",
    "timezone",
    "locales",
    "calendar_tags",
}


class ProjectMarketError(ValueError):
    pass


def market_payload(row: models.ProjectMarket) -> Dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "key": row.key,
        "country_code": row.country_code,
        "region_codes": list(row.region_codes or []),
        "timezone": row.timezone,
        "locales": list(row.locales or []),
        "calendar_tags": list(row.calendar_tags or []),
        "status": row.status,
        "is_default": bool(row.is_default),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class ProjectMarketService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def contract() -> Dict[str, Any]:
        return {
            "version": "1.0",
            "concept": (
                "One project may contain any practical number of commercial/calendar markets. "
                "Market is not locale: a market has country/regions/timezone/calendar semantics, "
                "while locales select content variants."
            ),
            "canonical_store": "project_markets",
            "legacy_projection": (
                "projects.market_config is synchronized for compatibility and must not be treated "
                "as the canonical write surface by new integrations."
            ),
            "stable_key_rule": "Market keys are immutable lowercase identifiers, never labels.",
            "default_rule": "Exactly one active market is default whenever active markets exist.",
            "archive_rule": (
                "Archiving is recoverable. When another active market exists, the default must be "
                "changed before it can be archived; archiving the sole active market leaves setup incomplete."
            ),
            "pagination": {
                "order": "key ascending",
                "cursor": "Pass next_cursor as after_key; page size is operational, not a market-count limit.",
                "maximum_page_size": 200,
            },
            "runtime_note": (
                "Creating a market changes configuration only. It does not reclassify contacts, "
                "activate automations, connect channels or authorize sends. Module-specific market "
                "overrides stay in their owning policy/channel/domain/destination records."
            ),
            "external_sends": 0,
        }

    def get_project(self, project_id: int) -> models.Project:
        project = self.db.query(models.Project).filter(models.Project.id == project_id).first()
        if not project:
            raise ProjectMarketError("Project not found")
        return project

    def get(self, project_id: int, key: str) -> Optional[models.ProjectMarket]:
        normalized = str(key or "").strip().lower()
        return self.db.query(models.ProjectMarket).filter(
            models.ProjectMarket.project_id == project_id,
            models.ProjectMarket.key == normalized,
        ).first()

    def list_page(
        self,
        project_id: int,
        *,
        include_archived: bool = False,
        after_key: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        bounded = max(1, min(int(limit), 200))
        query = self.db.query(models.ProjectMarket).filter(
            models.ProjectMarket.project_id == project_id
        )
        if not include_archived:
            query = query.filter(models.ProjectMarket.status == "active")
        if after_key:
            query = query.filter(models.ProjectMarket.key > str(after_key).strip().lower())
        rows = query.order_by(models.ProjectMarket.key.asc()).limit(bounded + 1).all()
        has_more = len(rows) > bounded
        page = rows[:bounded]
        return {
            "project_id": project_id,
            "markets": [market_payload(row) for row in page],
            "count": len(page),
            "has_more": has_more,
            "next_cursor": page[-1].key if has_more and page else None,
            "external_sends": 0,
        }

    @staticmethod
    def _normalized_market(project: models.Project, market: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(market, dict):
            raise ProjectMarketError("market must be an object")
        normalized = normalize_market_config(
            {"default_market_key": market.get("key"), "markets": [market]},
            project.supported_locales or [project.default_locale],
        )
        return normalized["markets"][0]

    def preview_create(self, project_id: int, market: Dict[str, Any], make_default: bool = False) -> Dict[str, Any]:
        project = self.get_project(project_id)
        normalized = self._normalized_market(project, market)
        if self.get(project_id, normalized["key"]):
            raise ProjectMarketError("Market key already exists; update or restore it instead")
        active_count = self.db.query(models.ProjectMarket).filter(
            models.ProjectMarket.project_id == project_id,
            models.ProjectMarket.status == "active",
        ).count()
        return {**normalized, "status": "active", "is_default": bool(make_default or active_count == 0)}

    def preview_update(self, project_id: int, key: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        row = self.get(project_id, key)
        if not row:
            raise ProjectMarketError("Market not found")
        if not isinstance(changes, dict) or not changes:
            raise ProjectMarketError("changes must contain at least one market field")
        unknown = set(changes) - MARKET_MUTABLE_FIELDS
        if unknown:
            raise ProjectMarketError(f"changes contains unsupported fields: {sorted(unknown)}")
        current = market_payload(row)
        candidate = {field: deepcopy(current[field]) for field in ["key", *sorted(MARKET_MUTABLE_FIELDS)]}
        candidate.update(deepcopy(changes))
        normalized = self._normalized_market(self.get_project(project_id), candidate)
        return {
            "before": current,
            "after": {**current, **normalized},
            "changes": {
                field: {"from": current[field], "to": normalized[field]}
                for field in MARKET_MUTABLE_FIELDS
                if current[field] != normalized[field]
            },
        }

    def _clear_default(self, project_id: int) -> None:
        rows = self.db.query(models.ProjectMarket).filter(
            models.ProjectMarket.project_id == project_id,
            models.ProjectMarket.is_default.is_(True),
        ).all()
        for row in rows:
            row.is_default = False
        if rows:
            self.db.flush()

    def _sync_legacy_projection(self, project: models.Project) -> None:
        rows = self.db.query(models.ProjectMarket).filter(
            models.ProjectMarket.project_id == project.id,
            models.ProjectMarket.status == "active",
        ).order_by(models.ProjectMarket.key.asc()).all()
        default = next((row for row in rows if row.is_default), None)
        if not rows:
            project.market_config = None
            return
        if not default:
            raise ProjectMarketError("Active markets require one default market")
        project.market_config = {
            "default_market_key": default.key,
            "markets": [
                {
                    "key": row.key,
                    "country_code": row.country_code,
                    "region_codes": list(row.region_codes or []),
                    "timezone": row.timezone,
                    "locales": list(row.locales or []),
                    "calendar_tags": list(row.calendar_tags or []),
                }
                for row in rows
            ],
        }

    def create(self, project_id: int, market: Dict[str, Any], make_default: bool = False) -> models.ProjectMarket:
        project = self.get_project(project_id)
        definition = {
            field: deepcopy(market[field])
            for field in ["key", *sorted(MARKET_MUTABLE_FIELDS)]
            if field in market
        }
        normalized = self.preview_create(project_id, definition, make_default)
        if normalized["is_default"]:
            self._clear_default(project_id)
        row = models.ProjectMarket(
            project_id=project_id,
            key=normalized["key"],
            country_code=normalized["country_code"],
            region_codes=normalized["region_codes"],
            timezone=normalized["timezone"],
            locales=normalized["locales"],
            calendar_tags=normalized["calendar_tags"],
            status="active",
            is_default=normalized["is_default"],
        )
        self.db.add(row)
        self.db.flush()
        self._sync_legacy_projection(project)
        return row

    def update(self, project_id: int, key: str, changes: Dict[str, Any]) -> models.ProjectMarket:
        report = self.preview_update(project_id, key, changes)
        row = self.get(project_id, key)
        for field in MARKET_MUTABLE_FIELDS:
            setattr(row, field, deepcopy(report["after"][field]))
        self.db.flush()
        self._sync_legacy_projection(self.get_project(project_id))
        return row

    def set_default(self, project_id: int, key: str) -> models.ProjectMarket:
        row = self.get(project_id, key)
        if not row:
            raise ProjectMarketError("Market not found")
        if row.status != "active":
            raise ProjectMarketError("Only an active market can be default")
        if row.is_default:
            return row
        self._clear_default(project_id)
        row.is_default = True
        self.db.flush()
        self._sync_legacy_projection(self.get_project(project_id))
        return row

    def set_status(self, project_id: int, key: str, status: str) -> models.ProjectMarket:
        target = str(status or "").strip().lower()
        if target not in MARKET_STATUSES:
            raise ProjectMarketError(f"status must be one of {sorted(MARKET_STATUSES)}")
        row = self.get(project_id, key)
        if not row:
            raise ProjectMarketError("Market not found")
        if row.status == target:
            return row
        if target == "archived" and row.is_default:
            another_active = self.db.query(models.ProjectMarket).filter(
                models.ProjectMarket.project_id == project_id,
                models.ProjectMarket.status == "active",
                models.ProjectMarket.id != row.id,
            ).first()
            if another_active:
                raise ProjectMarketError("Set another default market before archiving this one")
        row.status = target
        if target == "archived":
            row.is_default = False
        else:
            has_default = self.db.query(models.ProjectMarket).filter(
                models.ProjectMarket.project_id == project_id,
                models.ProjectMarket.status == "active",
                models.ProjectMarket.is_default.is_(True),
            ).first()
            if not has_default:
                row.is_default = True
        self.db.flush()
        self._sync_legacy_projection(self.get_project(project_id))
        return row

    def replace_from_config(self, project: models.Project, config: Optional[Dict[str, Any]]) -> None:
        """Dual-write a legacy bulk foundation update without deleting history."""
        rows = self.db.query(models.ProjectMarket).filter(
            models.ProjectMarket.project_id == project.id
        ).all()
        by_key = {row.key: row for row in rows}
        if config is None:
            for row in rows:
                row.status = "archived"
                row.is_default = False
            project.market_config = None
            self.db.flush()
            return
        normalized = normalize_market_config(
            config, project.supported_locales or [project.default_locale]
        )
        incoming = {item["key"]: item for item in normalized["markets"]}
        self._clear_default(project.id)
        for key, row in by_key.items():
            if key not in incoming:
                row.status = "archived"
                row.is_default = False
        for key, item in incoming.items():
            row = by_key.get(key)
            if row is None:
                row = models.ProjectMarket(project_id=project.id, key=key)
                self.db.add(row)
            for field in MARKET_MUTABLE_FIELDS:
                setattr(row, field, deepcopy(item[field]))
            row.status = "active"
            row.is_default = key == normalized["default_market_key"]
        self.db.flush()
        self._sync_legacy_projection(project)
