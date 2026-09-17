"""
Scheduled Action Worker for Event Actions

Background worker that processes scheduled/delayed actions.
Runs periodically to check for due actions and execute them.
"""
import logging
import asyncio
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import ScheduledEventAction, EventAction, EventActionExecution
from app.models.messaging import MessagingEvent, MessagingUser
from .executor import ActionExecutor, ActionContext

logger = logging.getLogger(__name__)


class ScheduledActionWorker:
    """
    Background worker that processes scheduled actions.

    Runs as an async background task, checking for due actions
    every N seconds and executing them.
    """

    def __init__(self, poll_interval: int = 10):
        """
        Initialize the worker.

        Args:
            poll_interval: Seconds between polls for due actions
        """
        self.poll_interval = poll_interval
        self.action_executor = ActionExecutor()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory) -> None:
        """
        Start the background worker.

        Args:
            db_session_factory: Factory function to create database sessions
        """
        if self._running:
            logger.warning("Scheduled action worker already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop(db_session_factory))
        logger.info("Scheduled action worker started")

    async def stop(self) -> None:
        """Stop the background worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduled action worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        """
        Main worker loop.

        Args:
            db_session_factory: Factory function to create database sessions
        """
        while self._running:
            try:
                db = db_session_factory()
                try:
                    processed = await self.process_due_actions(db)
                    if processed > 0:
                        logger.info(f"Processed {processed} scheduled actions")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in scheduled action worker: {e}")

            await asyncio.sleep(self.poll_interval)

    async def process_due_actions(self, db: Session) -> int:
        """
        Process all due scheduled actions.

        Args:
            db: Database session

        Returns:
            Number of actions processed
        """
        now = datetime.utcnow()

        # Find due actions
        due_actions = db.query(ScheduledEventAction).filter(
            and_(
                ScheduledEventAction.status == "pending",
                ScheduledEventAction.scheduled_for <= now
            )
        ).limit(100).all()  # Process in batches

        processed = 0
        for scheduled in due_actions:
            try:
                await self._execute_scheduled_action(db, scheduled)
                processed += 1
            except Exception as e:
                logger.error(f"Error executing scheduled action {scheduled.id}: {e}")
                scheduled.status = "cancelled"
                scheduled.cancelled_at = now
                scheduled.cancel_reason = f"Execution error: {str(e)}"
                scheduled.error_message = str(e)

        db.commit()
        return processed

    async def _execute_scheduled_action(
        self,
        db: Session,
        scheduled: ScheduledEventAction
    ) -> None:
        """
        Execute a single scheduled action.

        Args:
            db: Database session
            scheduled: The scheduled action to execute
        """
        now = datetime.utcnow()
        start_time = now

        event_action = db.query(EventAction).filter(
            EventAction.id == scheduled.event_action_id,
        ).first()
        if event_action:
            from app.services.orchestration_cutover_service import OrchestrationCutoverService
            gate = OrchestrationCutoverService(db).execution_gate(
                scheduled.project_id, event_action.purpose_key,
            )
            if not gate["allowed"]:
                scheduled.status = "cancelled"
                scheduled.cancelled_at = now
                scheduled.cancel_reason = (
                    f"Orchestration purpose owned by {gate['mode']} "
                    f"at epoch {gate['orchestration_epoch']}"
                )
                return

        # Get user and event data
        user_data = None
        if scheduled.user_id:
            user = db.query(MessagingUser).filter(
                MessagingUser.id == scheduled.user_id
            ).first()
            if user:
                user_data = {
                    "id": user.id,
                    "external_id": user.external_id,
                    "email": user.email,
                    "phone": user.phone,
                    "phone_e164": user.phone_e164,
                    "name": user.name,
                    "properties": user.properties or {}
                }

        event_data = {}
        if scheduled.event_id:
            event = db.query(MessagingEvent).filter(
                MessagingEvent.id == scheduled.event_id
            ).first()
            if event:
                event_data = {
                    "event_name": event.event_name,
                    "properties": event.properties or {},
                    "source": event.source
                }

        # Build context with stored variables
        variables = scheduled.variables or {}
        context = ActionContext(
            db=db,
            project_id=scheduled.project_id,
            user_id=scheduled.user_id,
            event_id=scheduled.event_id,
            event_data=event_data,
            user_data=user_data,
            variables=variables,
            event_action_id=scheduled.event_action_id,
        )

        # Execute the action
        action_config = scheduled.action_config
        result = await self.action_executor.execute(
            action_type=action_config.get("type"),
            config=action_config.get("config", {}),
            context=context
        )

        # Update scheduled action status
        end_time = datetime.utcnow()
        if result.success:
            scheduled.status = "executed"
            scheduled.executed_at = now
        else:
            scheduled.status = "cancelled"
            scheduled.cancelled_at = now
            scheduled.cancel_reason = "Execution failed"
            scheduled.error_message = result.error

        # Create execution record
        execution = EventActionExecution(
            event_action_id=scheduled.event_action_id,
            event_id=scheduled.event_id,
            user_id=scheduled.user_id,
            actions_executed=[{
                "index": scheduled.action_index,
                "type": action_config.get("type"),
                "status": "success" if result.success else "failed",
                "message": result.message,
                "scheduled": True,
                "scheduled_for": scheduled.scheduled_for.isoformat()
            }],
            status="success" if result.success else "failed",
            error_message=result.error,
            started_at=start_time,
            completed_at=end_time,
            duration_ms=int((end_time - start_time).total_seconds() * 1000)
        )
        db.add(execution)

        logger.info(
            f"Executed scheduled action {scheduled.id}: {result.success}"
        )

    async def cancel_scheduled_action(
        self,
        db: Session,
        scheduled_id: int,
        reason: str = "Manually cancelled"
    ) -> bool:
        """
        Cancel a scheduled action.

        Args:
            db: Database session
            scheduled_id: ID of scheduled action
            reason: Cancellation reason

        Returns:
            True if cancelled successfully
        """
        scheduled = db.query(ScheduledEventAction).filter(
            and_(
                ScheduledEventAction.id == scheduled_id,
                ScheduledEventAction.status == "pending"
            )
        ).first()

        if not scheduled:
            return False

        scheduled.status = "cancelled"
        scheduled.cancelled_at = datetime.utcnow()
        scheduled.cancel_reason = reason
        db.commit()

        return True

    async def get_pending_count(self, db: Session, project_id: Optional[int] = None) -> int:
        """
        Get count of pending scheduled actions.

        Args:
            db: Database session
            project_id: Optional project filter

        Returns:
            Number of pending actions
        """
        query = db.query(ScheduledEventAction).filter(
            ScheduledEventAction.status == "pending"
        )
        if project_id:
            query = query.filter(ScheduledEventAction.project_id == project_id)
        return query.count()


# Singleton instance
scheduled_action_worker = ScheduledActionWorker()
