"""
Event Action Engine

Core engine that processes events and executes matching actions.
Integrates with the EventProcessor to handle events and trigger actions.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import (
    EventAction, EventActionExecution, ScheduledEventAction,
    EventActionCooldown
)
from app.models.messaging import MessagingEvent, MessagingUser
from .conditions import ConditionEvaluator
from .executor import ActionExecutor, ActionContext, ActionResult

logger = logging.getLogger(__name__)


class EventActionEngine:
    """
    Core engine for processing events and executing matching actions.

    Flow:
    1. Event is received
    2. Find all active EventActions that match the trigger_event
    3. For each matching action:
        a. Check cooldown (deduplication)
        b. Evaluate conditions
        c. Execute immediate actions
        d. Schedule delayed actions
    4. Record execution in audit log
    """

    def __init__(self):
        self.condition_evaluator = ConditionEvaluator()
        self.action_executor = ActionExecutor()

    async def process_event(
        self,
        db: Session,
        event: MessagingEvent
    ) -> List[EventActionExecution]:
        """
        Process an event and execute matching Event Actions.

        Args:
            db: Database session
            event: The MessagingEvent to process

        Returns:
            List of EventActionExecution records
        """
        executions = []

        try:
            # Find matching event actions
            event_actions = self._find_matching_actions(db, event.project_id, event.event_name)

            if not event_actions:
                logger.debug(f"No event actions match event {event.event_name}")
                return executions

            # Get user if available
            user = None
            user_data = None
            if event.user_id:
                user = db.query(MessagingUser).filter(
                    MessagingUser.id == event.user_id
                ).first()
                if user:
                    properties = user.properties or {}
                    user_data = {
                        "id": user.id,
                        "external_id": user.external_id,
                        "email": user.email,
                        "phone": user.phone,
                        "phone_e164": user.phone_e164,
                        "name": user.name,
                        "locale": user.locale or properties.get("communication_locale"),
                        "communication_locale": properties.get("communication_locale") or user.locale,
                        "timezone": user.timezone or properties.get("timezone"),
                        "country": properties.get("country") or properties.get("market_country"),
                        "market_country": properties.get("market_country") or properties.get("country"),
                        "ui_language": properties.get("ui_language"),
                        "content_locale": properties.get("content_locale"),
                        "properties": properties,
                    }

            # Build event data for condition evaluation
            event_data = {
                "event_name": event.event_name,
                "properties": event.properties or {},
                "source": event.source,
                "created_at": event.created_at.isoformat() if event.created_at else None
            }

            # Process each matching action
            for action in event_actions:
                try:
                    execution = await self._process_action(
                        db, action, event, event_data, user, user_data
                    )
                    if execution:
                        executions.append(execution)
                except Exception as e:
                    logger.error(f"Error processing action {action.id}: {e}")
                    # Create failed execution record
                    execution = EventActionExecution(
                        event_action_id=action.id,
                        event_id=event.id,
                        user_id=event.user_id,
                        status="failed",
                        error_message=str(e),
                        started_at=datetime.utcnow(),
                        completed_at=datetime.utcnow()
                    )
                    db.add(execution)
                    executions.append(execution)

            db.commit()

        except Exception as e:
            logger.error(f"Error in event action engine: {e}")
            db.rollback()

        return executions

    async def _process_action(
        self,
        db: Session,
        action: EventAction,
        event: MessagingEvent,
        event_data: Dict[str, Any],
        user: Optional[MessagingUser],
        user_data: Optional[Dict[str, Any]]
    ) -> Optional[EventActionExecution]:
        """
        Process a single EventAction for an event.

        Args:
            db: Database session
            action: The EventAction rule
            event: The triggering event
            event_data: Parsed event data
            user: The associated user (if any)
            user_data: User data dict

        Returns:
            EventActionExecution record or None
        """
        start_time = datetime.utcnow()
        properties = event_data.get("properties") or {}
        playground_bypass_cooldown = (
            properties.get("playground_source") == "tabloide_admin_test"
            and properties.get("playground_bypass_cooldown") is True
        )

        # Check cooldown
        if user and not playground_bypass_cooldown and not self._check_cooldown(db, action.id, user.id, action.cooldown_seconds):
            logger.debug(f"Action {action.id} in cooldown for user {user.id}")
            return None

        # Inject user scores into condition context
        extra_ctx = {}
        if user and user.id:
            try:
                from app.services.scoring.scoring_engine import ScoringEngine
                snapshots = ScoringEngine(db).get_user_scores(event.project_id, user.id)
                score_data = {}
                for s in snapshots:
                    slug = s.score_definition.slug
                    score_data[slug] = {"value": s.score, "tier": s.tier}
                    extra_ctx[f"score.{slug}"] = s.score
                    extra_ctx[f"score.{slug}.tier"] = s.tier
                if score_data:
                    extra_ctx["score"] = score_data
            except Exception as e:
                logger.warning(f"Error loading scores for conditions: {e}")

            # Inject segment into condition context
            extra_ctx["segment"] = {
                "id": user.segment_rule_id if hasattr(user, 'segment_rule_id') else None,
                "name": user.segment_name if hasattr(user, 'segment_name') else None,
            }

        # Evaluate conditions
        if action.conditions:
            if not self.condition_evaluator.evaluate_all(
                action.conditions, event_data, user_data, extra_context=extra_ctx
            ):
                logger.debug(f"Conditions not met for action {action.id}")
                return None

        # Build context
        variables = self._build_variables(event_data, user_data, db=db, project_id=event.project_id)
        context = ActionContext(
            db=db,
            project_id=event.project_id,
            user_id=user.id if user else None,
            event_id=event.id,
            event_data=event_data,
            user_data=user_data,
            variables=variables,
            event_action_id=action.id,
        )

        # Execute actions
        executed_actions = []
        overall_status = "success"
        error_message = None

        for idx, action_config in enumerate(action.actions):
            delay_seconds = action_config.get("delay_seconds", 0)

            if delay_seconds > 0:
                # Schedule delayed action
                await self._schedule_action(
                    db, action, event, user, idx, action_config, variables, delay_seconds
                )
                executed_actions.append({
                    "index": idx,
                    "type": action_config.get("type"),
                    "status": "scheduled",
                    "delay_seconds": delay_seconds
                })
            else:
                # Execute immediately
                result = await self.action_executor.execute(
                    action_type=action_config.get("type"),
                    config=action_config.get("config", {}),
                    context=context
                )
                executed_actions.append({
                    "index": idx,
                    "type": action_config.get("type"),
                    "status": "success" if result.success else "failed",
                    "message": result.message,
                    "data": result.data
                })
                if not result.success:
                    overall_status = "partial"
                    if not error_message:
                        error_message = result.error

        # Update cooldown
        if user and not playground_bypass_cooldown:
            self._update_cooldown(db, action, user.id)

        # Create execution record
        end_time = datetime.utcnow()
        execution = EventActionExecution(
            event_action_id=action.id,
            event_id=event.id,
            user_id=user.id if user else None,
            actions_executed=executed_actions,
            status=overall_status,
            error_message=error_message,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=int((end_time - start_time).total_seconds() * 1000)
        )
        db.add(execution)

        return execution

    async def _schedule_action(
        self,
        db: Session,
        action: EventAction,
        event: MessagingEvent,
        user: Optional[MessagingUser],
        action_index: int,
        action_config: Dict[str, Any],
        variables: Dict[str, Any],
        delay_seconds: int
    ) -> ScheduledEventAction:
        """
        Schedule a delayed action.

        Args:
            db: Database session
            action: The EventAction rule
            event: The triggering event
            user: The associated user
            action_index: Index of this action in the actions array
            action_config: The action configuration
            variables: Rendered variables
            delay_seconds: Seconds to delay

        Returns:
            ScheduledEventAction record
        """
        scheduled_for = datetime.utcnow() + timedelta(seconds=delay_seconds)

        scheduled = ScheduledEventAction(
            project_id=event.project_id,
            event_action_id=action.id,
            user_id=user.id if user else None,
            event_id=event.id,
            action_index=action_index,
            action_config=action_config,
            variables=variables,
            scheduled_for=scheduled_for,
            status="pending"
        )
        db.add(scheduled)
        db.flush()

        logger.info(
            f"Scheduled action {action_index} of {action.id} for {scheduled_for}"
        )

        return scheduled

    async def check_stop_conditions_for_event(
        self,
        db: Session,
        event: MessagingEvent
    ) -> int:
        """
        Check if any scheduled actions should be cancelled due to stop conditions.

        When a new event occurs, check all scheduled actions for the same user
        to see if any have stop conditions that match this event.

        Args:
            db: Database session
            event: The new event

        Returns:
            Number of cancelled actions
        """
        cancelled_count = 0

        if not event.user_id:
            return cancelled_count

        # Find pending scheduled actions for this user
        pending_actions = db.query(ScheduledEventAction).filter(
            and_(
                ScheduledEventAction.project_id == event.project_id,
                ScheduledEventAction.user_id == event.user_id,
                ScheduledEventAction.status == "pending"
            )
        ).all()

        for scheduled in pending_actions:
            # Get the parent EventAction
            event_action = db.query(EventAction).filter(
                EventAction.id == scheduled.event_action_id
            ).first()

            if not event_action or not event_action.stop_conditions:
                continue

            # Check each stop condition
            for stop_condition in event_action.stop_conditions:
                stop_event = stop_condition.get("event")
                within_seconds = stop_condition.get("within_seconds", 86400)

                if stop_event == event.event_name:
                    # Check if within time window
                    time_since_scheduled = (datetime.utcnow() - scheduled.created_at).total_seconds()
                    if time_since_scheduled <= within_seconds:
                        scheduled.status = "cancelled"
                        scheduled.cancelled_at = datetime.utcnow()
                        scheduled.cancel_reason = f"Stop condition met: {stop_event}"
                        cancelled_count += 1
                        logger.info(
                            f"Cancelled scheduled action {scheduled.id} due to stop event {stop_event}"
                        )
                        break

        if cancelled_count > 0:
            db.commit()

        return cancelled_count

    def _find_matching_actions(
        self,
        db: Session,
        project_id: int,
        event_name: str
    ) -> List[EventAction]:
        """
        Find all active EventActions that match the event.

        Channel delivery events (channel.*) are only matched against
        actions that have react_to_delivery=True to prevent loops.

        Args:
            db: Database session
            project_id: Project ID
            event_name: Event name to match

        Returns:
            List of matching EventAction records, ordered by priority
        """
        is_channel_event = event_name.startswith("channel.")

        query = db.query(EventAction).filter(
            and_(
                EventAction.project_id == project_id,
                EventAction.trigger_event == event_name,
                EventAction.is_active == True
            )
        )

        # Guard: channel events only processed by opt-in actions
        if is_channel_event:
            query = query.filter(EventAction.react_to_delivery == True)

        actions = query.order_by(EventAction.priority.desc()).all()

        return actions

    def _check_cooldown(
        self,
        db: Session,
        action_id: int,
        user_id: int,
        cooldown_seconds: int
    ) -> bool:
        """
        Check if action can be triggered (not in cooldown).

        Args:
            db: Database session
            action_id: EventAction ID
            user_id: User ID
            cooldown_seconds: Cooldown period

        Returns:
            True if action can be triggered
        """
        cooldown = db.query(EventActionCooldown).filter(
            and_(
                EventActionCooldown.event_action_id == action_id,
                EventActionCooldown.user_id == user_id
            )
        ).first()

        if not cooldown:
            return True

        return datetime.utcnow() > cooldown.expires_at

    def _update_cooldown(
        self,
        db: Session,
        action: EventAction,
        user_id: int
    ) -> None:
        """
        Update or create cooldown record.

        Args:
            db: Database session
            action: The EventAction
            user_id: User ID
        """
        now = datetime.utcnow()
        expires_at = now + timedelta(seconds=action.cooldown_seconds)

        # Try to update existing
        cooldown = db.query(EventActionCooldown).filter(
            and_(
                EventActionCooldown.event_action_id == action.id,
                EventActionCooldown.user_id == user_id
            )
        ).first()

        if cooldown:
            cooldown.last_triggered_at = now
            cooldown.expires_at = expires_at
        else:
            cooldown = EventActionCooldown(
                project_id=action.project_id,
                event_action_id=action.id,
                user_id=user_id,
                last_triggered_at=now,
                expires_at=expires_at
            )
            db.add(cooldown)

    def _build_variables(
        self,
        event_data: Dict[str, Any],
        user_data: Optional[Dict[str, Any]],
        db: "Session | None" = None,
        project_id: "int | None" = None,
    ) -> Dict[str, Any]:
        """
        Build template variables from event and user data.

        Args:
            event_data: Event data
            user_data: User data

        Returns:
            Variables dict for template rendering
        """
        variables = {}

        # Add event properties
        if event_data.get("properties"):
            variables.update(event_data["properties"])

        # Add event metadata
        variables["event"] = {
            "name": event_data.get("event_name"),
            "source": event_data.get("source"),
            "timestamp": event_data.get("created_at")
        }

        # Add user data
        if user_data:
            name = user_data.get("name") or ""
            first_name = name.split()[0] if name else ""
            variables["user"] = {**user_data, "first_name": first_name}
            # Convenience aliases
            variables["name"] = name
            variables["first_name"] = first_name
            variables["email"] = user_data.get("email")
            variables["phone"] = user_data.get("phone")
            # Add user properties
            if user_data.get("properties"):
                variables.update(user_data["properties"])

        # Inject project variables
        if db and project_id:
            try:
                from app.services.project_variable_service import ProjectVariableService
                contact_ctx = {
                    "name": variables.get("name", ""),
                    "first_name": variables.get("first_name", ""),
                    "email": variables.get("email", ""),
                    "phone": variables.get("phone", ""),
                    "external_id": variables.get("external_id", ""),
                }
                if user_data and user_data.get("properties"):
                    contact_ctx.update(user_data["properties"])
                project_variables = ProjectVariableService(db).render_project_variables(
                    project_id, contact_ctx
                )
                variables["project"] = project_variables
                # Backward-compatible alias for templates authored with plural scope.
                variables["projects"] = project_variables
            except Exception as e:
                logger.warning(f"Error injecting project variables: {e}")

        return variables


# Singleton instance
event_action_engine = EventActionEngine()
