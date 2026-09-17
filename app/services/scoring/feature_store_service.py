"""
Feature Store Service — Incremental event aggregation.

Maintains per-user event aggregates without scanning the event table.
Uses INSERT ON CONFLICT DO UPDATE for upserts.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from sqlalchemy.orm import Session
from sqlalchemy import and_, text

from app.models import UserFeatureStore
from app.models.messaging import MessagingEvent

logger = logging.getLogger(__name__)

# Reserved event_name prefix for client traits (e.g. "client:timezone") —
# NOT event aggregates. Excluded from batch drift-refresh and merge count
# summing; last_value carries the trait payload.
TRAIT_PREFIX = "client:"


class FeatureStoreService:
    def __init__(self, db: Session):
        self.db = db

    def set_trait(
        self,
        project_id: int,
        user_id: int,
        key: str,
        value: Dict[str, Any],
        changed_fields: Optional[List[str]] = None,
    ) -> None:
        """Upsert a client-trait row (reserved event_name, e.g. "client:timezone").

        Writes only when the trait materially changed: compares last_value on
        changed_fields (default: all keys of value except observation
        timestamps) so per-event calls are cheap no-ops.
        """
        if not key.startswith(TRAIT_PREFIX):
            raise ValueError(f"trait key must start with '{TRAIT_PREFIX}'")

        now = datetime.utcnow()
        existing = self.db.query(UserFeatureStore).filter(
            UserFeatureStore.project_id == project_id,
            UserFeatureStore.user_id == user_id,
            UserFeatureStore.event_name == key,
        ).first()

        if existing:
            current = existing.last_value or {}
            fields = changed_fields or [k for k in value if k != "observed_at"]
            if all(current.get(f) == value.get(f) for f in fields):
                return  # unchanged — no write
            existing.last_value = value
            existing.last_seen_at = now
            self.db.flush()
            return

        self.db.add(UserFeatureStore(
            project_id=project_id,
            user_id=user_id,
            event_name=key,
            count_total=1, count_1d=1, count_7d=1, count_30d=1,
            sum_value=0,
            last_value=value,
            first_seen_at=now,
            last_seen_at=now,
        ))
        self.db.flush()

    def update_features_for_event(
        self,
        project_id: int,
        user_id: int,
        event_name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> UserFeatureStore:
        """
        Incrementally update feature store for a single event.
        Uses upsert logic to avoid scanning the events table.
        """
        now = datetime.utcnow()

        existing = self.db.query(UserFeatureStore).filter(
            UserFeatureStore.project_id == project_id,
            UserFeatureStore.user_id == user_id,
            UserFeatureStore.event_name == event_name,
        ).first()

        if existing:
            existing.count_total += 1
            existing.count_1d += 1
            existing.count_7d += 1
            existing.count_30d += 1

            # Track numeric value from properties if present
            value = self._extract_numeric_value(properties)
            if value is not None:
                existing.sum_value += value

            existing.last_value = properties
            existing.last_seen_at = now
            return existing

        # Create new feature row
        feature = UserFeatureStore(
            project_id=project_id,
            user_id=user_id,
            event_name=event_name,
            count_total=1,
            count_1d=1,
            count_7d=1,
            count_30d=1,
            sum_value=self._extract_numeric_value(properties) or 0,
            last_value=properties,
            first_seen_at=now,
            last_seen_at=now,
        )
        self.db.add(feature)
        self.db.flush()
        return feature

    def get_user_features(
        self,
        project_id: int,
        user_id: int,
    ) -> List[UserFeatureStore]:
        """Get all feature rows for a user."""
        return self.db.query(UserFeatureStore).filter(
            UserFeatureStore.project_id == project_id,
            UserFeatureStore.user_id == user_id,
        ).all()

    def batch_refresh_project(self, project_id: int) -> int:
        """
        Correct window count drift by recalculating count_1d/7d/30d
        from the raw events table. Called periodically by the scheduler.
        """
        now = datetime.utcnow()
        cutoff_1d = now - timedelta(days=1)
        cutoff_7d = now - timedelta(days=7)
        cutoff_30d = now - timedelta(days=30)

        features = self.db.query(UserFeatureStore).filter(
            UserFeatureStore.project_id == project_id,
        ).all()

        refreshed = 0
        for feature in features:
            # Trait rows (client:*) are not event aggregates — recounting
            # from messaging_events would zero them out. Skip.
            if feature.event_name.startswith(TRAIT_PREFIX):
                continue
            # Count events in each window
            count_1d = self.db.query(MessagingEvent).filter(
                MessagingEvent.project_id == project_id,
                MessagingEvent.user_id == feature.user_id,
                MessagingEvent.event_name == feature.event_name,
                MessagingEvent.created_at >= cutoff_1d,
            ).count()

            count_7d = self.db.query(MessagingEvent).filter(
                MessagingEvent.project_id == project_id,
                MessagingEvent.user_id == feature.user_id,
                MessagingEvent.event_name == feature.event_name,
                MessagingEvent.created_at >= cutoff_7d,
            ).count()

            count_30d = self.db.query(MessagingEvent).filter(
                MessagingEvent.project_id == project_id,
                MessagingEvent.user_id == feature.user_id,
                MessagingEvent.event_name == feature.event_name,
                MessagingEvent.created_at >= cutoff_30d,
            ).count()

            feature.count_1d = count_1d
            feature.count_7d = count_7d
            feature.count_30d = count_30d
            refreshed += 1

        if refreshed:
            self.db.commit()

        logger.info(f"Refreshed {refreshed} feature rows for project {project_id}")
        return refreshed

    @staticmethod
    def _extract_numeric_value(properties: Optional[Dict[str, Any]]) -> Optional[float]:
        """Extract a numeric value from properties for sum aggregation."""
        if not properties:
            return None
        for key in ("value", "amount", "price", "total", "revenue"):
            val = properties.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return None
