"""
Inbox Expiry Worker — background worker for auto-expiring stale support tickets.

Polls every 60 seconds for tickets that have exceeded their project's TTL
(inbox_ttl_waiting_agent for unassigned, inbox_ttl_waiting_customer for waiting_customer),
expires them, and triggers escalation return logic.
"""
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal

logger = logging.getLogger(__name__)


class InboxExpiryWorker:
    """Background worker that expires stale support tickets."""

    def __init__(self, poll_interval: int = 60):
        self.poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, db_session_factory=None) -> None:
        if self._running:
            logger.warning("Inbox expiry worker already running")
            return

        self._running = True
        factory = db_session_factory or SessionLocal
        self._task = asyncio.create_task(self._run_loop(factory))
        logger.info("Inbox expiry worker started (poll=%ds)", self.poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Inbox expiry worker stopped")

    async def _run_loop(self, db_session_factory) -> None:
        while self._running:
            try:
                db = db_session_factory()
                try:
                    expired_count = self._process_cycle(db)
                    if expired_count > 0:
                        logger.info(f"Inbox expiry worker: expired {expired_count} ticket(s)")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Inbox expiry worker error: {e}", exc_info=True)

            await asyncio.sleep(self.poll_interval)

    def _process_cycle(self, db: Session) -> int:
        """Process one expiry cycle. Returns number of tickets expired."""
        from app.models import SupportTicket, Project

        now = datetime.utcnow()
        total_expired = 0

        # Find projects that have at least one TTL configured
        projects = db.query(Project).filter(
            (Project.inbox_ttl_waiting_agent.isnot(None)) |
            (Project.inbox_ttl_waiting_customer.isnot(None)),
        ).all()

        for project in projects:
            # Expire unassigned "open" tickets past waiting_agent TTL
            if project.inbox_ttl_waiting_agent:
                cutoff = now - timedelta(minutes=project.inbox_ttl_waiting_agent)
                stale_tickets = db.query(SupportTicket).filter(
                    SupportTicket.project_id == project.id,
                    SupportTicket.status == "open",
                    SupportTicket.assigned_to_user_id.is_(None),
                    SupportTicket.created_at < cutoff,
                ).all()

                for ticket in stale_tickets:
                    total_expired += self._expire_ticket(db, ticket, "ttl_waiting_agent")

            # Expire "waiting_customer" tickets past waiting_customer TTL
            if project.inbox_ttl_waiting_customer:
                cutoff = now - timedelta(minutes=project.inbox_ttl_waiting_customer)
                stale_tickets = db.query(SupportTicket).filter(
                    SupportTicket.project_id == project.id,
                    SupportTicket.status == "waiting_customer",
                    SupportTicket.updated_at < cutoff,
                ).all()

                for ticket in stale_tickets:
                    total_expired += self._expire_ticket(db, ticket, "ttl_waiting_customer")

        return total_expired

    def _expire_ticket(self, db: Session, ticket, reason: str) -> int:
        """Expire a single ticket and trigger escalation return."""
        try:
            ticket.status = "expired"
            ticket.resolved_at = datetime.utcnow()
            ticket.internal_notes = (ticket.internal_notes or "") + f"\n[Auto-expired: {reason}]"
            db.commit()

            # Trigger escalation return (resume funnel, restore chatbot, etc.)
            try:
                from app.services.support_inbox_service import SupportInboxService
                inbox_service = SupportInboxService(db)
                inbox_service._handle_escalation_return(ticket)
            except Exception as e:
                logger.warning(f"Escalation return failed for expired ticket {ticket.id}: {e}")

            logger.info(
                f"Expired ticket {ticket.ticket_number} (project={ticket.project_id}, "
                f"reason={reason})"
            )
            return 1
        except Exception as e:
            logger.error(f"Error expiring ticket {ticket.id}: {e}")
            db.rollback()
            return 0


# Singleton instance
inbox_expiry_worker = InboxExpiryWorker(poll_interval=60)
