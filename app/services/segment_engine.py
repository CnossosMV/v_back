"""
Segment Engine — evaluates priority-ordered segment rules against contacts.

Contacts are classified into the first matching segment (by priority).
Segment transitions emit a 'segment_changed' event for downstream automations.
"""
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from app.models import SegmentRule
from app.models.messaging import MessagingUser, MessagingEvent

logger = logging.getLogger(__name__)


class SegmentEngine:
    def __init__(self, db: Session):
        self.db = db

    def evaluate_user(
        self,
        project_id: int,
        user_id: int,
        trigger: str = "event",
    ) -> Dict[str, Any]:
        """
        Evaluate all active segment rules for a single user.
        First match (by priority) wins. Updates user columns if changed.

        Returns dict with previous/new segment info and changed flag.
        """
        from app.services.event_actions.conditions import ConditionEvaluator

        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()

        if not user:
            return {"changed": False, "error": "user_not_found"}

        rules = self.db.query(SegmentRule).filter(
            SegmentRule.project_id == project_id,
            SegmentRule.is_active == True,
        ).order_by(SegmentRule.priority).all()

        if not rules:
            # No rules — clear segment if set
            if user.segment_rule_id is not None:
                prev = {"id": user.segment_rule_id, "name": user.segment_name}
                user.segment_rule_id = None
                user.segment_name = None
                user.segment_updated_at = datetime.utcnow()
                self.db.flush()
                self._emit_segment_changed(project_id, user.id, prev, None, trigger)
                return {"changed": True, "previous": prev, "new": None}
            return {"changed": False}

        context = self._build_segment_context(project_id, user)
        evaluator = ConditionEvaluator()

        matched_rule = None
        for rule in rules:
            if rule.is_catch_all:
                matched_rule = rule
                break

            conditions = rule.conditions or []
            if not conditions:
                continue

            if evaluator.evaluate_all(
                conditions=conditions,
                event_data={},
                user_data=None,
                match_mode=rule.match_mode,
                extra_context=context,
            ):
                matched_rule = rule
                break

        previous_id = user.segment_rule_id
        previous_name = user.segment_name
        new_id = matched_rule.id if matched_rule else None
        new_name = matched_rule.name if matched_rule else None

        if previous_id != new_id:
            user.segment_rule_id = new_id
            user.segment_name = new_name
            user.segment_updated_at = datetime.utcnow()
            self.db.flush()

            prev = {"id": previous_id, "name": previous_name} if previous_id else None
            new = {"id": new_id, "name": new_name} if new_id else None
            self._emit_segment_changed(project_id, user.id, prev, new, trigger)

            return {"changed": True, "previous": prev, "new": new}

        return {"changed": False, "segment": {"id": new_id, "name": new_name}}

    def _build_segment_context(
        self,
        project_id: int,
        user: MessagingUser,
    ) -> Dict[str, Any]:
        """
        Build nested context dict for ConditionEvaluator.
        Paths like user.email, event.page_view.count_30d, score.intent.value
        all resolve via _get_nested_value.
        """
        from app.services.scoring.feature_store_service import FeatureStoreService
        from app.services.scoring.scoring_engine import ScoringEngine
        from app.models import ScoreDefinition

        # User data
        user_data = {
            "id": user.id,
            "external_id": user.external_id,
            "email": user.email,
            "phone": user.phone,
            "name": user.name,
        }
        if isinstance(user.properties, dict):
            user_data.update(user.properties)

        # Event aggregates from feature store
        event_data = {}
        try:
            fs = FeatureStoreService(self.db)
            features = fs.get_user_features(project_id, user.id)
            for f in features:
                days_since = None
                if f.last_seen_at:
                    delta = datetime.utcnow() - f.last_seen_at
                    days_since = delta.days

                event_data[f.event_name] = {
                    "count_total": f.count_total or 0,
                    "count_1d": f.count_1d or 0,
                    "count_7d": f.count_7d or 0,
                    "count_30d": f.count_30d or 0,
                    "sum_value": f.sum_value or 0,
                    "has_done": (f.count_total or 0) > 0,
                    "days_since_last": days_since,
                    "last_seen_at": f.last_seen_at.isoformat() if f.last_seen_at else None,
                }
        except Exception as e:
            logger.warning(f"Error loading feature store for user {user.id}: {e}")

        # Score data
        score_data = {}
        try:
            scoring = ScoringEngine(self.db)
            snapshots = scoring.get_user_scores(project_id, user.id)
            for snap in snapshots:
                defn = self.db.query(ScoreDefinition).filter(
                    ScoreDefinition.id == snap.score_definition_id
                ).first()
                if defn:
                    score_data[defn.slug] = {
                        "value": snap.score,
                        "tier": snap.tier,
                    }
        except Exception as e:
            logger.warning(f"Error loading scores for user {user.id}: {e}")

        # Segment context
        segment_data = {
            "id": user.segment_rule_id,
            "name": user.segment_name,
        }

        return {
            "user": user_data,
            "event": event_data,
            "score": score_data,
            "segment": segment_data,
        }

    def _emit_segment_changed(
        self,
        project_id: int,
        user_id: int,
        previous: Optional[Dict],
        new: Optional[Dict],
        trigger: str,
    ) -> None:
        """Create a segment_changed event for downstream processing."""
        event = MessagingEvent(
            project_id=project_id,
            user_id=user_id,
            event_name="segment_changed",
            source="system",
            properties={
                "previous_segment_id": previous["id"] if previous else None,
                "previous_segment_name": previous["name"] if previous else None,
                "new_segment_id": new["id"] if new else None,
                "new_segment_name": new["name"] if new else None,
                "changed_at": datetime.utcnow().isoformat(),
                "evaluation_trigger": trigger,
            },
            processed=False,
        )
        self.db.add(event)
        self.db.flush()
        logger.info(
            f"Segment changed for user {user_id}: "
            f"{previous['name'] if previous else 'None'} → "
            f"{new['name'] if new else 'None'}"
        )

    def batch_evaluate_project(
        self,
        project_id: int,
        chunk_size: int = 500,
    ) -> Dict[str, Any]:
        """
        Evaluate all contacts in a project against the segment rules.
        Processes in chunks to avoid memory issues on large projects.
        """
        start = time.time()
        transitions = 0
        total = 0

        user_ids = (
            self.db.query(MessagingUser.id)
            .filter(MessagingUser.project_id == project_id)
            .all()
        )
        user_ids = [uid[0] for uid in user_ids]

        for i in range(0, len(user_ids), chunk_size):
            chunk = user_ids[i : i + chunk_size]
            for uid in chunk:
                try:
                    result = self.evaluate_user(project_id, uid, trigger="batch")
                    total += 1
                    if result.get("changed"):
                        transitions += 1
                except Exception as e:
                    logger.error(f"Error evaluating user {uid}: {e}")
                    total += 1

            self.db.commit()

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            f"Batch evaluation for project {project_id}: "
            f"{total} users, {transitions} transitions, {duration_ms}ms"
        )
        return {
            "total_evaluated": total,
            "transitions": transitions,
            "duration_ms": duration_ms,
        }

    def preview_rules(self, project_id: int) -> Tuple[List[Dict[str, Any]], int]:
        """
        Dry-run evaluation — count how many contacts would match each rule
        without writing any changes.
        """
        from app.services.event_actions.conditions import ConditionEvaluator

        rules = self.db.query(SegmentRule).filter(
            SegmentRule.project_id == project_id,
            SegmentRule.is_active == True,
        ).order_by(SegmentRule.priority).all()

        if not rules:
            return []

        users = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
        ).all()

        evaluator = ConditionEvaluator()
        counts = {rule.id: 0 for rule in rules}
        unmatched = 0

        for user in users:
            context = self._build_segment_context(project_id, user)
            matched = False

            for rule in rules:
                if rule.is_catch_all:
                    counts[rule.id] += 1
                    matched = True
                    break

                conditions = rule.conditions or []
                if not conditions:
                    continue

                if evaluator.evaluate_all(
                    conditions=conditions,
                    event_data={},
                    user_data=None,
                    match_mode=rule.match_mode,
                    extra_context=context,
                ):
                    counts[rule.id] += 1
                    matched = True
                    break

            if not matched:
                unmatched += 1

        items = []
        for rule in rules:
            items.append({
                "rule_id": rule.id,
                "rule_name": rule.name,
                "contact_count": counts[rule.id],
            })

        return items, unmatched

    def test_user(
        self,
        project_id: int,
        user_id: int,
    ) -> Dict[str, Any]:
        """
        Test segment evaluation for a specific user.
        Returns per-rule match/no-match results without modifying data.
        """
        from app.services.event_actions.conditions import ConditionEvaluator

        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()

        if not user:
            return {"error": "user_not_found"}

        rules = self.db.query(SegmentRule).filter(
            SegmentRule.project_id == project_id,
            SegmentRule.is_active == True,
        ).order_by(SegmentRule.priority).all()

        context = self._build_segment_context(project_id, user)
        evaluator = ConditionEvaluator()

        results = []
        evaluated_segment = None

        for rule in rules:
            if rule.is_catch_all:
                matched = True
            else:
                conditions = rule.conditions or []
                if not conditions:
                    matched = False
                else:
                    matched = evaluator.evaluate_all(
                        conditions=conditions,
                        event_data={},
                        user_data=None,
                        match_mode=rule.match_mode,
                        extra_context=context,
                    )

            if matched and evaluated_segment is None:
                evaluated_segment = rule.name

            results.append({
                "rule_id": rule.id,
                "rule_name": rule.name,
                "priority": rule.priority,
                "matched": matched,
                "is_catch_all": rule.is_catch_all,
            })

        return {
            "user_id": user.id,
            "current_segment": user.segment_name,
            "evaluated_segment": evaluated_segment,
            "results": results,
        }
