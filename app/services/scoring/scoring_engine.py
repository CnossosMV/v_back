"""
Scoring Engine — Computes user scores from feature store aggregates.

Supports weighted signals, recency decay, normalization, and tier assignment.
"""
import math
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import ScoreDefinition, UserFeatureStore, UserScoreSnapshot
from .feature_store_service import FeatureStoreService

logger = logging.getLogger(__name__)


class ScoringEngine:
    def __init__(self, db: Session):
        self.db = db
        self.feature_store = FeatureStoreService(db)

    def process_event(
        self,
        project_id: int,
        user_id: int,
        event_name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> List[UserScoreSnapshot]:
        """
        Full pipeline: update features → find relevant definitions → recalculate.
        Called from event_processor after each event.
        """
        # 1. Update feature store
        self.feature_store.update_features_for_event(
            project_id, user_id, event_name, properties
        )

        # 2. Find active definitions that care about this event and recalc_on_event=True
        definitions = self.db.query(ScoreDefinition).filter(
            ScoreDefinition.project_id == project_id,
            ScoreDefinition.status == "active",
            ScoreDefinition.recalc_on_event == True,
        ).all()

        # Filter to definitions that reference this event in signals
        relevant = []
        for defn in definitions:
            signals = defn.signals or []
            for signal in signals:
                if signal.get("event_name") == event_name:
                    relevant.append(defn)
                    break

        # 3. Recalculate scores
        snapshots = []
        for defn in relevant:
            try:
                snapshot = self.recalculate_user_score(defn, user_id)
                if snapshot:
                    snapshots.append(snapshot)
            except Exception as e:
                logger.error(f"Error recalculating score {defn.slug} for user {user_id}: {e}")

        if snapshots:
            self.db.flush()

        return snapshots

    def recalculate_user_score(
        self,
        definition: ScoreDefinition,
        user_id: int,
    ) -> Optional[UserScoreSnapshot]:
        """
        Load features → apply weights + decay → normalize → determine tier → upsert snapshot.
        """
        features = self.feature_store.get_user_features(definition.project_id, user_id)
        if not features:
            return None

        feature_map = {f.event_name: f for f in features}

        signals = definition.signals or []
        if not signals:
            return None

        now = datetime.utcnow()
        total_raw = 0.0
        explanation = []

        for signal_cfg in signals:
            event_name = signal_cfg.get("event_name", "")
            weight = signal_cfg.get("weight", 1.0)
            aggregate = signal_cfg.get("aggregate", "count")
            window_days = signal_cfg.get("window_days")
            max_contribution = signal_cfg.get("max_contribution")
            label = signal_cfg.get("label", event_name)

            feature = feature_map.get(event_name)
            if not feature:
                explanation.append({
                    "signal": label,
                    "event_name": event_name,
                    "raw_value": 0,
                    "weight": weight,
                    "decay_factor": 1.0,
                    "weighted_value": 0,
                    "contribution_pct": 0,
                })
                continue

            # Get aggregate value
            raw_value = self._get_aggregate_value(feature, aggregate, window_days)

            # Compute decay factor
            decay_type = signal_cfg.get("decay_type")
            half_life_days = signal_cfg.get("half_life_days")
            if not decay_type and definition.decay_config:
                decay_type = definition.decay_config.get("type")
                half_life_days = definition.decay_config.get("half_life_days")

            decay_factor = self._compute_decay(
                feature.last_seen_at, decay_type, half_life_days, now
            )

            weighted = raw_value * weight * decay_factor

            # Apply max contribution cap
            if max_contribution is not None:
                weighted = min(weighted, max_contribution)

            total_raw += weighted

            explanation.append({
                "signal": label,
                "event_name": event_name,
                "raw_value": round(raw_value, 2),
                "weight": weight,
                "decay_factor": round(decay_factor, 4),
                "weighted_value": round(weighted, 2),
                "contribution_pct": 0,  # filled after normalization
            })

        # Normalize to 0-max range
        norm_max = definition.normalization_max or 100
        score = min(total_raw, norm_max)
        score = max(score, 0)

        # Fill contribution percentages
        if total_raw > 0:
            for item in explanation:
                item["contribution_pct"] = round(
                    (item["weighted_value"] / total_raw) * 100, 1
                )

        # Determine tier
        thresholds = definition.thresholds or {"hot": 70, "warm": 40, "cold": 0}
        tier = self._determine_tier(score, thresholds)

        # Upsert snapshot
        existing = self.db.query(UserScoreSnapshot).filter(
            UserScoreSnapshot.user_id == user_id,
            UserScoreSnapshot.score_definition_id == definition.id,
        ).first()

        if existing:
            existing.previous_score = existing.score
            existing.score_delta = round(score - existing.score, 2)
            existing.score = round(score, 2)
            existing.tier = tier
            existing.explanation = explanation
            existing.calculated_at = now
            return existing

        snapshot = UserScoreSnapshot(
            project_id=definition.project_id,
            user_id=user_id,
            score_definition_id=definition.id,
            score=round(score, 2),
            tier=tier,
            explanation=explanation,
            previous_score=None,
            score_delta=None,
            calculated_at=now,
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot

    def batch_recalculate(self, definition_id: int) -> int:
        """Recalculate scores for all users that have features for this definition."""
        definition = self.db.query(ScoreDefinition).filter(
            ScoreDefinition.id == definition_id,
        ).first()
        if not definition:
            return 0

        # Get all unique user_ids with features for this project
        user_ids = (
            self.db.query(UserFeatureStore.user_id)
            .filter(UserFeatureStore.project_id == definition.project_id)
            .distinct()
            .all()
        )

        count = 0
        for (uid,) in user_ids:
            try:
                self.recalculate_user_score(definition, uid)
                count += 1
            except Exception as e:
                logger.error(f"Error batch recalculating user {uid}: {e}")

        definition.last_recalc_at = datetime.utcnow()
        self.db.commit()
        logger.info(f"Batch recalculated {count} users for definition {definition.slug}")
        return count

    def get_user_scores(
        self,
        project_id: int,
        user_id: int,
    ) -> List[UserScoreSnapshot]:
        """Get all score snapshots for a user."""
        return (
            self.db.query(UserScoreSnapshot)
            .join(ScoreDefinition)
            .filter(
                UserScoreSnapshot.project_id == project_id,
                UserScoreSnapshot.user_id == user_id,
                ScoreDefinition.status == "active",
            )
            .all()
        )

    @staticmethod
    def _get_aggregate_value(
        feature: UserFeatureStore,
        aggregate: str,
        window_days: Optional[int],
    ) -> float:
        """Pick the appropriate aggregate value based on window."""
        if aggregate == "exists":
            return 1.0 if feature.count_total > 0 else 0.0

        if aggregate == "sum":
            return feature.sum_value or 0.0

        if aggregate == "last_value":
            if feature.last_value and isinstance(feature.last_value, dict):
                for key in ("value", "amount", "score"):
                    val = feature.last_value.get(key)
                    if val is not None:
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            pass
            return 0.0

        # Default: count
        if window_days is None:
            return float(feature.count_total)
        elif window_days <= 1:
            return float(feature.count_1d)
        elif window_days <= 7:
            return float(feature.count_7d)
        elif window_days <= 30:
            return float(feature.count_30d)
        return float(feature.count_total)

    @staticmethod
    def _compute_decay(
        last_seen: Optional[datetime],
        decay_type: Optional[str],
        half_life_days: Optional[float],
        now: Optional[datetime] = None,
    ) -> float:
        """Compute recency decay factor (0-1)."""
        if not decay_type or not last_seen or not half_life_days:
            return 1.0

        now = now or datetime.utcnow()
        days_ago = (now - last_seen).total_seconds() / 86400.0

        if days_ago <= 0:
            return 1.0

        if decay_type == "exponential":
            return math.pow(0.5, days_ago / half_life_days)
        elif decay_type == "linear":
            return max(0.0, 1.0 - (days_ago / (half_life_days * 2)))

        return 1.0

    @staticmethod
    def _determine_tier(
        score: float,
        thresholds: Dict[str, Any],
    ) -> str:
        hot = thresholds.get("hot", 70)
        warm = thresholds.get("warm", 40)
        if score >= hot:
            return "hot"
        elif score >= warm:
            return "warm"
        return "cold"
