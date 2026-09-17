"""
Support Inbox Service

Handles support ticket management, agent assignment, and conversation takeover.
"""

import asyncio
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_, desc

from app.models import (
    SupportTicket, ChatSession, ChatMessage, User, Chatbot, Project, MessagingUser
)
from app.services.realtime.redis_pubsub import publish_inbox_event
from app.services.realtime import event_types as rt
from app.services.push_notification_service import get_push_service

logger = logging.getLogger(__name__)


class SupportInboxService:
    """Service for managing support inbox and tickets."""

    def __init__(self, db: Session):
        """
        Initialize the support inbox service.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def _publish(self, project_id: int, event_type: str, data: dict):
        """Fire-and-forget publish to Redis."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(publish_inbox_event(project_id, event_type, data))
        except RuntimeError:
            pass  # No running loop (e.g., in tests)

    def _push_to_agents(self, project_id: int, title: str, body: str, data: dict = None):
        """Send push notification to all support agents on this project."""
        try:
            push_svc = get_push_service(self.db)
            push_svc.send_to_project_agents(project_id, title, body, data)
        except Exception as e:
            logger.error(f"Push notification error: {e}")

    def _push_to_user(self, user_id: int, title: str, body: str, data: dict = None):
        """Send push notification to a specific user."""
        try:
            push_svc = get_push_service(self.db)
            push_svc.send_notification(user_id, title, body, data)
        except Exception as e:
            logger.error(f"Push notification error: {e}")

    def _insert_system_message(self, ticket: SupportTicket, event_type: str, content: str) -> ChatMessage:
        """Insert a system ChatMessage as a ticket lifecycle marker (opened/resolved/closed)."""
        msg = ChatMessage(
            session_id=ticket.session_id,
            role="system",
            sender_type="system",
            content=content,
            channel=ticket.channel,
            message_metadata={
                "event_type": event_type,
                "ticket_id": ticket.id,
                "ticket_number": ticket.ticket_number,
            },
        )
        self.db.add(msg)
        self.db.flush()
        self._publish(ticket.project_id, rt.MESSAGE_SENT, {
            "ticket_id": ticket.id,
            "ticket_number": ticket.ticket_number,
            "message_id": msg.id,
            "content": content,
            "role": "system",
            "sender_type": "system",
            "channel": msg.channel,
            "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
            "message_metadata": msg.message_metadata,
        })
        return msg

    def _ticket_dict(self, ticket: SupportTicket) -> dict:
        """Serialize ticket to dict for events."""
        assigned_name = (
            ticket.assigned_to.name
            if ticket.assigned_to_user_id and ticket.assigned_to
            else None
        )
        return {
            "id": ticket.id,
            "project_id": ticket.project_id,
            "ticket_number": ticket.ticket_number,
            "status": ticket.status,
            "priority": ticket.priority,
            "assigned_to_user_id": ticket.assigned_to_user_id,
            "assigned_to_user_name": assigned_name,
            "customer_name": ticket.customer_name,
            "customer_identifier": ticket.customer_identifier,
            "channel": ticket.channel,
            "human_takeover": ticket.human_takeover,
        }

    def create_ticket(
        self,
        session_id: int,
        reason: Optional[str] = None,
        priority: str = "medium",
        tags: Optional[List[str]] = None,
        escalation_origin: Optional[Dict[str, Any]] = None,
        contact_id: Optional[int] = None,
        notification_title: Optional[str] = None,
        notification_body: Optional[str] = None,
        allow_duplicate: bool = False,
    ) -> SupportTicket:
        """
        Create a new support ticket for a session.

        Args:
            session_id: Chat session ID
            reason: Reason for creating the ticket
            priority: Ticket priority (low, medium, high, urgent)
            tags: Optional tags for the ticket
            allow_duplicate: If True, skip dedup check (for approval tickets that need one ticket per message)

        Returns:
            Created SupportTicket
        """
        session = self.db.query(ChatSession).filter(
            ChatSession.id == session_id
        ).first()

        if not session:
            raise ValueError(f"Session {session_id} not found")

        # Check for existing open ticket (skip for approval duplicates)
        if not allow_duplicate:
            existing = self.db.query(SupportTicket).filter(
                SupportTicket.session_id == session_id,
                SupportTicket.status.in_(["open", "in_progress", "waiting_customer"])
            ).first()

            if existing:
                return existing

        # Generate ticket number
        ticket_number = self._generate_ticket_number(session.chatbot.project_id)

        resolved_contact_id = contact_id or session.contact_id

        ticket = SupportTicket(
            project_id=session.chatbot.project_id,
            chatbot_id=session.chatbot_id,
            session_id=session_id,
            ticket_number=ticket_number,
            status="open",
            priority=priority,
            human_takeover=True,
            bot_can_resume=True,
            customer_identifier=session.user_identifier,
            channel=session.channel,
            escalation_reason=reason,
            escalation_origin=escalation_origin,
            tags=tags or [],
            contact_id=resolved_contact_id,
        )

        # Enrich customer details from MessagingUser if available
        if resolved_contact_id:
            contact = self.db.query(MessagingUser).filter(
                MessagingUser.id == resolved_contact_id
            ).first()
            if contact:
                ticket.customer_name = contact.name or None
                if not ticket.customer_phone:
                    ticket.customer_phone = contact.phone or None

        self.db.add(ticket)

        # Update session
        session.human_takeover = True
        session.human_takeover_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(ticket)

        self._insert_system_message(ticket, "ticket_opened", f"Ticket #{ticket.ticket_number} opened")
        self.db.commit()

        self._publish(ticket.project_id, rt.TICKET_CREATED, self._ticket_dict(ticket))
        self._push_to_agents(
            ticket.project_id,
            notification_title or "New Support Ticket",
            notification_body or f"#{ticket.ticket_number} — {ticket.customer_name or ticket.customer_identifier}",
            {"url": f"/support/{ticket.id}", "ticket_id": ticket.id, "project_id": ticket.project_id},
        )

        return ticket

    def assign_ticket(
        self,
        ticket_id: int,
        user_id: int
    ) -> SupportTicket:
        """
        Assign a ticket to a user.

        Args:
            ticket_id: Ticket ID
            user_id: User ID to assign to

        Returns:
            Updated SupportTicket
        """
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.id == ticket_id
        ).first()

        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")

        ticket.assigned_to_user_id = user_id
        if ticket.status == "open":
            ticket.status = "in_progress"

        self.db.commit()
        self.db.refresh(ticket)

        # Resolve agent name
        agent = self.db.query(User).filter(User.id == user_id).first()
        agent_name = agent.name if agent else f"User {user_id}"

        # Insert system message for assignment
        self._insert_system_message(
            ticket, "ticket_assigned", f"Ticket assigned to {agent_name}"
        )
        self.db.commit()

        self._publish(ticket.project_id, rt.TICKET_ASSIGNED, {
            **self._ticket_dict(ticket),
            "assigned_to_user_id": user_id,
            "assigned_to_user_name": agent_name,
        })
        self._push_to_user(
            user_id,
            "Ticket Assigned",
            f"#{ticket.ticket_number} has been assigned to you",
            {"url": f"/support/{ticket.id}", "ticket_id": ticket.id, "project_id": ticket.project_id},
        )

        return ticket

    def take_over_conversation(
        self,
        ticket_id: int,
        user_id: int
    ) -> SupportTicket:
        """
        Take over a conversation as a human agent.

        Args:
            ticket_id: Ticket ID
            user_id: User ID taking over

        Returns:
            Updated SupportTicket
        """
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.id == ticket_id
        ).first()

        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")

        # Track previous assignee for conflict detection
        previous_assignee_id = ticket.assigned_to_user_id
        previous_assignee_name = None
        if previous_assignee_id and previous_assignee_id != user_id:
            prev_user = self.db.query(User).filter(User.id == previous_assignee_id).first()
            previous_assignee_name = prev_user.name if prev_user else None

        ticket.assigned_to_user_id = user_id
        ticket.status = "in_progress"
        ticket.human_takeover = True

        if not ticket.first_response_at:
            ticket.first_response_at = datetime.utcnow()

        # Update session
        session = ticket.session
        session.human_takeover = True
        session.human_takeover_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(ticket)

        # Resolve new agent name
        agent = self.db.query(User).filter(User.id == user_id).first()
        agent_name = agent.name if agent else f"User {user_id}"

        # Insert system message
        self._insert_system_message(
            ticket, "ticket_assigned", f"Ticket assigned to {agent_name}"
        )
        self.db.commit()

        # Publish TICKET_ASSIGNED event
        event_data = {
            **self._ticket_dict(ticket),
            "assigned_to_user_id": user_id,
            "assigned_to_user_name": agent_name,
        }
        if previous_assignee_id and previous_assignee_id != user_id:
            event_data["previous_assignee_id"] = previous_assignee_id
            event_data["previous_assignee_name"] = previous_assignee_name
        self._publish(ticket.project_id, rt.TICKET_ASSIGNED, event_data)

        return ticket

    def release_to_bot(
        self,
        ticket_id: int,
        resolve: bool = True
    ) -> SupportTicket:
        """
        Release a conversation back to the bot.

        Args:
            ticket_id: Ticket ID
            resolve: Whether to mark the ticket as resolved

        Returns:
            Updated SupportTicket
        """
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.id == ticket_id
        ).first()

        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")

        if not ticket.bot_can_resume:
            raise ValueError("Bot cannot resume this conversation")

        ticket.human_takeover = False
        if resolve:
            ticket.status = "resolved"
            ticket.resolved_at = datetime.utcnow()
            self._insert_system_message(ticket, "ticket_resolved", f"Ticket #{ticket.ticket_number} resolved")

        # Update session
        session = ticket.session
        session.human_takeover = False
        session.bot_resumed_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(ticket)

        # Handle return to the original handler (funnel resume, chatbot/agent_team restore)
        if resolve:
            self._handle_escalation_return(ticket)

        return ticket

    def _handle_escalation_return(self, ticket: SupportTicket) -> None:
        """After resolving a ticket, handle return to the original handler.

        Reads ticket.escalation_origin to determine what to restore:
        - funnel: resume or exit the enrollment
        - chatbot: restore chatbot routing state
        - agent_team: restore agent_team routing state
        - funnel_approval: send the approved message (debug mode)

        Also falls back to legacy pause-based resume for tickets without escalation_origin.
        """
        try:
            origin = ticket.escalation_origin
            if not origin:
                # Agent-initiated tickets have no escalation origin, but their
                # human routing state still must be released when resolved.
                self._clear_human_routing(ticket)
                # Legacy fallback: check paused enrollments by metadata
                self._legacy_paused_enrollment_resume(ticket)
                return

            origin_type = origin.get("type")
            return_action = origin.get("return_action", "resume")
            ref_id = origin.get("ref_id")
            step_id = origin.get("step_id")

            if origin_type == "funnel":
                from app.services.funnel_engine import FunnelEngine
                engine = FunnelEngine(self.db)

                if return_action == "exit":
                    engine.return_from_human(ref_id, step_id=step_id, action="exit")
                    logger.info(f"Funnel enrollment {ref_id} exited after ticket {ticket.id} resolved")
                elif return_action == "resume":
                    engine.return_from_human(ref_id, step_id=step_id, action="resume")
                    logger.info(f"Funnel enrollment {ref_id} resumed after ticket {ticket.id} resolved")
                # return_action == "ignore" → do nothing

                # Clear human routing state
                self._clear_human_routing(ticket)

            elif origin_type == "chatbot":
                # Restore chatbot routing state
                self._restore_handler_routing(ticket, "chatbot", ref_id)

            elif origin_type == "agent_team":
                # Restore agent_team routing state
                self._restore_handler_routing(ticket, "agent_team", ref_id)

            elif origin_type == "funnel_approval":
                # Debug mode approval — handled separately by approval endpoints
                pass

            else:
                logger.warning(f"Unknown escalation_origin type '{origin_type}' on ticket {ticket.id}")

        except Exception as e:
            logger.error(f"Error handling escalation return for ticket {ticket.id}: {e}", exc_info=True)

    def _clear_human_routing(self, ticket: SupportTicket) -> None:
        """Clear the human routing state row for the ticket's contact."""
        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            state_svc = RoutingStateService(self.db)
            state_svc.clear_state(
                project_id=ticket.project_id,
                identifier=ticket.customer_identifier,
                channel=ticket.channel,
            )
        except Exception as e:
            logger.warning(f"Error clearing human routing state: {e}")

    def _restore_handler_routing(self, ticket: SupportTicket, handler_type: str, handler_id: int) -> None:
        """Clear human routing state and restore the previous handler."""
        try:
            from app.services.inbound.routing_state_service import RoutingStateService
            state_svc = RoutingStateService(self.db)
            # Clear the human row
            state_svc.clear_state(
                project_id=ticket.project_id,
                identifier=ticket.customer_identifier,
                channel=ticket.channel,
            )
            # Set the previous handler back
            if handler_id:
                state_svc.set_handler(
                    project_id=ticket.project_id,
                    identifier=ticket.customer_identifier,
                    channel=ticket.channel,
                    handler_type=handler_type,
                    handler_id=handler_id,
                    session_id=ticket.session_id,
                )
        except Exception as e:
            logger.warning(f"Error restoring {handler_type} routing state: {e}")

    def _legacy_paused_enrollment_resume(self, ticket: SupportTicket) -> None:
        """Legacy fallback: check paused enrollments by metadata (pre-escalation_origin tickets)."""
        try:
            from app.models import FunnelEnrollment
            from app.services.funnel_engine import FunnelEngine

            paused_enrollments = self.db.query(FunnelEnrollment).filter(
                FunnelEnrollment.status == "active",
                FunnelEnrollment.enrollment_metadata["_is_paused"].astext == "true",
            ).all()

            engine = FunnelEngine(self.db)
            for enrollment in paused_enrollments:
                meta = enrollment.enrollment_metadata or {}
                for key, val in meta.items():
                    if key.startswith("_pause_") and isinstance(val, dict) and val.get("ticket_id") == ticket.id:
                        result = engine.resume_enrollment(enrollment.id)
                        logger.info(f"Paused enrollment {enrollment.id} resumed after ticket {ticket.id} resolved: {result}")
                        return
        except Exception as e:
            logger.error(f"Error in legacy paused enrollment resume: {e}", exc_info=True)

    def update_ticket(
        self,
        ticket_id: int,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        tags: Optional[List[str]] = None,
        internal_notes: Optional[str] = None,
        bot_can_resume: Optional[bool] = None
    ) -> SupportTicket:
        """
        Update ticket details.

        Args:
            ticket_id: Ticket ID
            status: New status
            priority: New priority
            tags: New tags
            internal_notes: Internal notes
            bot_can_resume: Whether bot can resume

        Returns:
            Updated SupportTicket
        """
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.id == ticket_id
        ).first()

        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")

        if status:
            ticket.status = status
            if status == "resolved":
                ticket.resolved_at = datetime.utcnow()
                self._insert_system_message(ticket, "ticket_resolved", f"Ticket #{ticket.ticket_number} resolved")
            elif status == "closed":
                ticket.closed_at = datetime.utcnow()
                self._insert_system_message(ticket, "ticket_closed", f"Ticket #{ticket.ticket_number} closed")

        if priority:
            ticket.priority = priority

        if tags is not None:
            ticket.tags = tags

        if internal_notes:
            ticket.internal_notes = internal_notes

        if bot_can_resume is not None:
            ticket.bot_can_resume = bot_can_resume

        self.db.commit()
        self.db.refresh(ticket)

        self._publish(ticket.project_id, rt.TICKET_UPDATED, self._ticket_dict(ticket))

        # When status changes to "resolved", handle return to original handler
        # (funnel resume, chatbot/agent_team restore) — same as release_to_bot()
        if status == "resolved":
            self._handle_escalation_return(ticket)
        elif status == "closed":
            self._clear_human_routing(ticket)

        return ticket

    def get_inbox(
        self,
        project_id: int,
        filters: Optional[Dict[str, Any]] = None,
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """
        Get inbox tickets with filters.

        Args:
            project_id: Project ID
            filters: Optional filters (status, priority, channel, assigned_to, etc.)
            page: Page number
            page_size: Page size

        Returns:
            Dict with tickets and pagination info
        """
        filters = filters or {}

        query = self.db.query(SupportTicket).options(
            joinedload(SupportTicket.assigned_to)
        ).filter(
            SupportTicket.project_id == project_id
        )

        # Apply filters
        if "status" in filters:
            if isinstance(filters["status"], list):
                query = query.filter(SupportTicket.status.in_(filters["status"]))
            else:
                query = query.filter(SupportTicket.status == filters["status"])

        if "priority" in filters:
            query = query.filter(SupportTicket.priority == filters["priority"])

        if "channel" in filters:
            query = query.filter(SupportTicket.channel == filters["channel"])

        if "assigned_to_user_id" in filters:
            query = query.filter(
                SupportTicket.assigned_to_user_id == filters["assigned_to_user_id"]
            )

        if "unassigned" in filters and filters["unassigned"]:
            query = query.filter(SupportTicket.assigned_to_user_id.is_(None))

        if "chatbot_id" in filters:
            query = query.filter(SupportTicket.chatbot_id == filters["chatbot_id"])

        if "search" in filters:
            search = f"%{filters['search']}%"
            query = query.filter(
                or_(
                    SupportTicket.ticket_number.ilike(search),
                    SupportTicket.customer_identifier.ilike(search),
                    SupportTicket.customer_name.ilike(search)
                )
            )

        # Get total count
        total = query.count()

        # Order by priority and creation date
        priority_order = {
            "urgent": 1,
            "high": 2,
            "medium": 3,
            "low": 4
        }
        query = query.order_by(
            SupportTicket.status.asc(),  # Open first
            desc(SupportTicket.created_at)
        )

        # Paginate
        offset = (page - 1) * page_size
        tickets = query.offset(offset).limit(page_size).all()

        return {
            "tickets": tickets,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }

    def get_ticket_with_messages(
        self,
        ticket_id: int
    ) -> Dict[str, Any]:
        """
        Get a ticket with its messages.

        Args:
            ticket_id: Ticket ID

        Returns:
            Dict with ticket and messages
        """
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.id == ticket_id
        ).first()

        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")

        messages = self.db.query(ChatMessage).filter(
            ChatMessage.session_id == ticket.session_id
        ).order_by(ChatMessage.timestamp.asc()).all()

        return {
            "ticket": ticket,
            "messages": messages,
            "session": ticket.session
        }

    def send_agent_message(
        self,
        ticket_id: int,
        user_id: int,
        content: str,
        media_url: Optional[str] = None,
        media_type: Optional[str] = None,
        filename: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatMessage:
        """
        Send a message as a support agent.

        Args:
            ticket_id: Ticket ID
            user_id: Agent user ID
            content: Message content

        Returns:
            Created ChatMessage
        """
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.id == ticket_id
        ).first()

        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")

        if not ticket.human_takeover:
            raise ValueError("Cannot send agent message without taking over")

        # Update first response time
        if not ticket.first_response_at:
            ticket.first_response_at = datetime.utcnow()

        # Build content_pieces for media messages
        content_pieces = None
        if media_url and media_type:
            content_pieces = [{
                "type": media_type,
                "media_url": media_url,
                "body": content,
            }]
        if attachments:
            attachment_pieces = [
                {"type": "document", "media_url": att.get("url"), "body": att.get("filename")}
                for att in attachments if att.get("url")
            ]
            if attachment_pieces:
                content_pieces = (content_pieces or []) + attachment_pieces

        # Create message
        message = ChatMessage(
            session_id=ticket.session_id,
            role="assistant",
            content=content,
            sender_type="human_agent",
            sent_by_user_id=user_id,
            content_pieces=content_pieces,
            channel=ticket.channel,
        )

        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)

        # Resolve agent name for WS event
        agent_name = self.db.query(User).filter(User.id == user_id).first()
        sent_by_user_name = agent_name.name if agent_name else None

        self._publish(ticket.project_id, rt.MESSAGE_SENT, {
            "ticket_id": ticket.id,
            "ticket_number": ticket.ticket_number,
            "message_id": message.id,
            "content": content,
            "content_pieces": content_pieces,
            "sender_type": "human_agent",
            "channel": message.channel,
            "user_id": user_id,
            "sent_by_user_name": sent_by_user_name,
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        })

        return message

    def get_inbox_stats(
        self,
        project_id: int
    ) -> Dict[str, Any]:
        """
        Get inbox statistics.

        Args:
            project_id: Project ID

        Returns:
            Dict with statistics
        """
        base_query = self.db.query(SupportTicket).filter(
            SupportTicket.project_id == project_id
        )

        total = base_query.count()
        open_count = base_query.filter(SupportTicket.status == "open").count()
        in_progress = base_query.filter(SupportTicket.status == "in_progress").count()
        waiting = base_query.filter(SupportTicket.status == "waiting_customer").count()
        resolved = base_query.filter(SupportTicket.status == "resolved").count()
        closed = base_query.filter(SupportTicket.status == "closed").count()

        urgent = base_query.filter(
            SupportTicket.priority == "urgent",
            SupportTicket.status.in_(["open", "in_progress"])
        ).count()

        unassigned = base_query.filter(
            SupportTicket.assigned_to_user_id.is_(None),
            SupportTicket.status.in_(["open", "in_progress"])
        ).count()

        return {
            "total": total,
            "open": open_count,
            "in_progress": in_progress,
            "waiting_customer": waiting,
            "resolved": resolved,
            "closed": closed,
            "urgent": urgent,
            "unassigned": unassigned
        }

    def publish_delivery_status(
        self,
        project_id: int,
        ticket_id: int,
        message_id: int,
        delivery_status: str,
        error: str = None,
    ):
        """Publish a delivery status update via WebSocket."""
        self._publish(project_id, rt.DELIVERY_STATUS_UPDATED, {
            "ticket_id": ticket_id,
            "message_id": message_id,
            "delivery_status": delivery_status,
            "error": error,
        })

    def _generate_ticket_number(self, project_id: int) -> str:
        """Generate a unique ticket number."""
        count = self.db.query(SupportTicket).filter(
            SupportTicket.project_id == project_id
        ).count()

        return f"PROJ{project_id}-{count + 1:04d}"


    # ── Inbox-initiated conversation ────────────────────────────────────

    def _get_or_create_inbox_system_chatbot(self, project_id: int) -> Chatbot:
        """Return an archived system chatbot used to anchor inbox-initiated sessions.

        ChatSession requires a non-null chatbot_id. Inbox-initiated conversations
        don't have a real chatbot, so each project gets exactly one archived
        'Inbox System' bot that the list endpoint already filters out.
        """
        bot = (
            self.db.query(Chatbot)
            .filter(
                Chatbot.project_id == project_id,
                Chatbot.intention == "__inbox_system__",
            )
            .first()
        )
        if bot:
            return bot
        bot = Chatbot(
            project_id=project_id,
            name="Inbox System",
            intention="__inbox_system__",
            status="archived",
            model_provider="openai",
            model_name="gpt-4o-mini",
        )
        self.db.add(bot)
        self.db.flush()
        return bot

    def _validate_start_conversation_payload(
        self,
        channel: str,
        provider_type: Optional[str],
        mode: str,
        template,
        body: Optional[str],
        subject: Optional[str],
    ) -> None:
        """Raise ValueError with a user-facing message if the payload is invalid."""
        if channel not in ("whatsapp", "sms", "email"):
            raise ValueError(f"Unsupported channel: {channel}")
        if mode not in ("template", "text", "html"):
            raise ValueError(f"Unsupported mode: {mode}")
        if mode == "template" and not template:
            raise ValueError("template_id is required when mode='template'")
        if mode in ("text", "html") and not body:
            raise ValueError(f"body is required when mode='{mode}'")
        if mode == "html" and channel != "email":
            raise ValueError("HTML mode is only supported for the email channel")

        if channel == "whatsapp":
            if provider_type == "meta_cloud_api":
                if mode != "template" or not template or not template.meta_template_name:
                    raise ValueError(
                        "Meta Cloud API requires a template with meta_template_name set "
                        "(free-form text is only allowed inside an open 24h window)."
                    )
            elif provider_type == "evolution_api":
                if mode == "template" and not template:
                    raise ValueError("template_id is required when mode='template'")
            else:
                raise ValueError(f"Unknown WhatsApp provider: {provider_type}")
            if template and template.channel_type and template.channel_type.value != "whatsapp":
                raise ValueError("Selected template is not a WhatsApp template")

        if channel == "sms":
            if mode != "template" or not template:
                raise ValueError("SMS requires a template (most carriers require pre-registered content).")
            if template.channel_type and template.channel_type.value != "sms":
                raise ValueError("Selected template is not an SMS template")

        if channel == "email":
            if mode in ("text", "html") and not subject:
                raise ValueError("Email free-text/HTML requires a subject")
            if template and template.channel_type and template.channel_type.value != "email":
                raise ValueError("Selected template is not an email template")

    async def start_conversation(
        self,
        project_id: int,
        agent_user_id: int,
        contact_id: int,
        channel: str,
        mode: str,
        template_id: Optional[int] = None,
        body: Optional[str] = None,
        subject: Optional[str] = None,
        whatsapp_instance_id: Optional[int] = None,
        email_instance_id: Optional[int] = None,
        smtp_config_id: Optional[int] = None,
        from_email: Optional[str] = None,
        from_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        variable_mapping: Optional[Dict[str, Any]] = None,
        priority: str = "medium",
        reason: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Start a new outbound conversation from the Inbox.

        Creates ChatSession + SupportTicket + ContactRoutingState and dispatches
        the first message through SendService. Returns identifiers the frontend
        needs to navigate to the newly-created ticket.
        """
        from app.models.messaging import MessagingTemplate
        from app.models import (
            ContactRoutingState,
            CustomerSMTPConfig,
            EmailInstance,
            Project,
            WhatsAppInstance,
        )
        from app.services.channels.base import OutboundContent
        from app.services.channels.send_service import SendService
        from app.services.messaging.template_renderer import template_renderer

        contact = self.db.query(MessagingUser).filter(
            MessagingUser.id == contact_id,
            MessagingUser.project_id == project_id,
        ).first()
        if not contact:
            raise ValueError(f"Contact {contact_id} not found in project {project_id}")
        if contact.is_subscribed is False:
            raise ValueError("Contact is unsubscribed")

        template = None
        if template_id:
            template = self.db.query(MessagingTemplate).filter(
                MessagingTemplate.id == template_id,
                MessagingTemplate.project_id == project_id,
                MessagingTemplate.is_active.is_(True),
            ).first()
            if not template:
                raise ValueError(f"Template {template_id} not found or inactive")

        # Resolve WhatsApp instance + provider_type for validation
        wa_instance = None
        provider_type: Optional[str] = None
        if channel == "whatsapp":
            wa_id = whatsapp_instance_id or (template.whatsapp_instance_id if template else None)
            if not wa_id:
                raise ValueError("whatsapp_instance_id is required for WhatsApp channel")
            wa_instance = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.id == wa_id,
                WhatsAppInstance.project_id == project_id,
            ).first()
            if not wa_instance:
                raise ValueError(f"WhatsApp instance {wa_id} not found")
            provider_type = wa_instance.provider_type

        # Resolve the explicitly selected email credential and enforce tenant scope.
        email_instance = None
        smtp_config = None
        if channel == "email":
            if email_instance_id and smtp_config_id:
                raise ValueError("Select only one email sender")

            project = self.db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise ValueError(f"Project {project_id} not found")

            if email_instance_id:
                email_instance = self.db.query(EmailInstance).filter(
                    EmailInstance.id == email_instance_id,
                    EmailInstance.workspace_id == project.workspace_id,
                    EmailInstance.is_active.is_(True),
                ).first()
                if not email_instance or (
                    email_instance.project_id is not None
                    and email_instance.project_id != project_id
                ):
                    raise ValueError("Selected email instance is not available for this project")
            elif smtp_config_id:
                smtp_config = self.db.query(CustomerSMTPConfig).filter(
                    CustomerSMTPConfig.id == smtp_config_id,
                    CustomerSMTPConfig.project_id == project_id,
                    CustomerSMTPConfig.is_active.is_(True),
                ).first()
                if not smtp_config:
                    raise ValueError("Selected SMTP configuration is not available for this project")

        self._validate_start_conversation_payload(
            channel=channel, provider_type=provider_type, mode=mode,
            template=template, body=body, subject=subject,
        )

        # Resolve recipient
        recipient: Optional[str] = None
        if channel in ("whatsapp", "sms"):
            recipient = contact.phone_e164 or contact.phone
        elif channel == "email":
            recipient = contact.email
        if not recipient:
            raise ValueError(f"Contact has no {('phone' if channel != 'email' else 'email')} on file")

        # Session + ticket bootstrap
        bot = self._get_or_create_inbox_system_chatbot(project_id)
        user_identifier = recipient
        session = ChatSession(
            chatbot_id=bot.id,
            user_id=None,
            user_identifier=user_identifier,
            channel=channel,
            handler_type="human",
            human_takeover=True,
            human_takeover_at=datetime.utcnow(),
            contact_id=contact.id,
        )
        self.db.add(session)
        self.db.flush()

        ticket_number = self._generate_ticket_number(project_id)
        ticket = SupportTicket(
            project_id=project_id,
            session_id=session.id,
            ticket_number=ticket_number,
            status="open",
            priority=priority,
            human_takeover=True,
            assigned_to_user_id=agent_user_id,
            customer_identifier=user_identifier,
            customer_name=contact.name,
            customer_phone=contact.phone,
            channel=channel,
            escalation_reason=reason or "Agent-initiated conversation",
            contact_id=contact.id,
        )
        self.db.add(ticket)
        self.db.flush()

        # Bond the session to the ticket for human takeover reverse lookups.
        session.support_ticket_id = ticket.id
        self.db.flush()

        # Build OutboundContent from validated payload
        variables: Dict[str, Any] = {}
        if variable_mapping:
            for k, v in variable_mapping.items():
                variables[k] = v
        rendered_subject: Optional[str] = subject
        rendered_body: str = body or ""
        if template:
            rb, rs, _, _ = template_renderer.render_template(
                template.body, variables, template.subject,
            )
            rendered_body = rb
            if rs:
                rendered_subject = rs

        content: OutboundContent
        instance_config: Optional[Dict[str, Any]] = None
        metadata: Dict[str, Any] = {}

        if channel == "whatsapp":
            instance_config = {"instance_id": wa_instance.id}
            if provider_type == "meta_cloud_api":
                # Always template for Meta
                from app.services.event_actions.executor import _render_meta_components
                rendered_components = _render_meta_components(template.meta_components or [], variables)
                content = OutboundContent(
                    content_type="template",
                    template_name=template.meta_template_name,
                    template_language=template.meta_language or "en_US",
                    template_components=rendered_components,
                )
            else:
                # Evolution: either rendered template body or free text — both as plain text
                content = OutboundContent(content_type="text", text=rendered_body)
        elif channel == "sms":
            content = OutboundContent(content_type="text", text=rendered_body)
        else:  # email
            tpl_body_format = getattr(template, "body_format", None) if template else None
            if mode == "html":
                content = OutboundContent(
                    content_type="rich", html=rendered_body, subject=rendered_subject,
                )
            elif tpl_body_format == "plain" or not template:
                content = OutboundContent(
                    content_type="text", text=rendered_body, subject=rendered_subject,
                )
            else:
                content = OutboundContent(
                    content_type="rich", html=rendered_body, subject=rendered_subject,
                )
            # Explicit request overrides win over template defaults.
            for key in ("from_email", "from_name", "reply_to"):
                val = {
                    "from_email": from_email,
                    "from_name": from_name,
                    "reply_to": reply_to,
                }[key]
                if not val:
                    val = (variable_mapping or {}).get(key) if variable_mapping else None
                if not val and template is not None:
                    val = getattr(template, key, None)
                if val:
                    metadata[key] = val
            if attachments:
                content.attachments = attachments
            if email_instance is not None:
                instance_config = {"instance_id": email_instance_id}
            elif smtp_config is not None:
                instance_config = {"smtp_config_id": smtp_config_id}
            if metadata:
                content.metadata = {**content.metadata, **metadata}
                instance_config = {**(instance_config or {}), **metadata}

        svc = SendService(self.db)
        decision = await svc.send(
            project_id=project_id,
            user_id=contact.id,
            recipient=recipient,
            content=content,
            channel=channel,
            source_type="support",
            source_id=ticket.id,
            instance_config=instance_config,
            template_id=template.id if template else None,
        )

        if not decision.success:
            # Keep the ticket in 'open' state but flag the send failure on the ticket.
            ticket.status = "open"
            ticket.escalation_reason = (ticket.escalation_reason or "") + f" | send failed: {decision.error}"
            self.db.commit()
            return {
                "ticket_id": ticket.id,
                "session_id": session.id,
                "send_log_id": decision.send_log_id,
                "status": "send_failed",
                "error": decision.error,
            }

        # Record the outbound as a ChatMessage so the conversation view renders it
        msg_body = rendered_body
        if channel == "email" and rendered_subject:
            msg_body = f"[{rendered_subject}]\n{rendered_body}"
        attachment_pieces = None
        if channel == "email" and attachments:
            attachment_pieces = [
                {"type": "document", "media_url": att.get("url"), "body": att.get("filename")}
                for att in attachments if att.get("url")
            ] or None
        message = ChatMessage(
            session_id=session.id,
            role="assistant",
            content=msg_body,
            sender_type="human_agent",
            sent_by_user_id=agent_user_id,
            channel=channel,
            content_pieces=attachment_pieces,
        )
        self.db.add(message)
        ticket.first_response_at = datetime.utcnow()

        # Route future inbound replies from this contact to this ticket's human handler.
        # This state remains active until the ticket is explicitly resolved/closed.
        routing = (
            self.db.query(ContactRoutingState)
            .filter(
                ContactRoutingState.project_id == project_id,
                ContactRoutingState.contact_identifier == user_identifier,
                ContactRoutingState.channel == channel,
                ContactRoutingState.handler_type.notin_(("funnel_wait", "funnel_send")),
            )
            .first()
        )
        if routing:
            routing.handler_id = ticket.id
            routing.handler_priority = 100
            routing.session_id = session.id
            routing.assigned_at = datetime.utcnow()
            routing.expires_at = None
        else:
            routing = ContactRoutingState(
                project_id=project_id,
                contact_identifier=user_identifier,
                channel=channel,
                handler_type="human",
                handler_id=ticket.id,
                handler_priority=100,
                session_id=session.id,
                expires_at=None,
            )
            self.db.add(routing)

        self.db.commit()
        self.db.refresh(ticket)
        self.db.refresh(message)

        self._publish(project_id, rt.TICKET_CREATED, self._ticket_dict(ticket))
        return {
            "ticket_id": ticket.id,
            "session_id": session.id,
            "send_log_id": decision.send_log_id,
            "status": "sent",
        }

    def reachable_channels(self, project_id: int, contact_id: int) -> List[Dict[str, Any]]:
        """Report which channels can reach a given contact and why not, when applicable."""
        from app.services.channels.channel_registry_service import ChannelRegistryService

        contact = self.db.query(MessagingUser).filter(
            MessagingUser.id == contact_id,
            MessagingUser.project_id == project_id,
        ).first()
        if not contact:
            raise ValueError(f"Contact {contact_id} not found")

        reg = ChannelRegistryService(self.db)
        results: List[Dict[str, Any]] = []

        def _entry(channel: str, reachable: bool, reason: Optional[str] = None) -> Dict[str, Any]:
            return {"channel": channel, "reachable": reachable, "reason": reason}

        # WhatsApp
        if not reg.is_channel_available(project_id, "whatsapp"):
            results.append(_entry("whatsapp", False, "channel not configured"))
        elif contact.is_subscribed is False:
            results.append(_entry("whatsapp", False, "contact is unsubscribed"))
        elif not (contact.phone_e164 or contact.phone):
            results.append(_entry("whatsapp", False, "no phone on file"))
        else:
            results.append(_entry("whatsapp", True))

        # SMS
        if not reg.is_channel_available(project_id, "sms"):
            results.append(_entry("sms", False, "channel not configured"))
        elif contact.is_subscribed is False:
            results.append(_entry("sms", False, "contact is unsubscribed"))
        elif not (contact.phone_e164 or contact.phone):
            results.append(_entry("sms", False, "no phone on file"))
        else:
            results.append(_entry("sms", True))

        # Email
        if not reg.is_channel_available(project_id, "email"):
            results.append(_entry("email", False, "channel not configured"))
        elif contact.is_subscribed is False:
            results.append(_entry("email", False, "contact is unsubscribed"))
        elif not contact.email:
            results.append(_entry("email", False, "no email on file"))
        else:
            results.append(_entry("email", True))

        # Messenger / Instagram — need a channel identity (PSID/IGSID) and an
        # open 24h window (or Human Agent tag for human replies).
        from app.models.messaging import ContactIdentity

        meta_identities: Dict[str, str] = {}
        try:
            ci_records = self.db.query(ContactIdentity).filter(
                ContactIdentity.user_id == contact.id,
                ContactIdentity.identity_type == "channel",
            ).all()
            for ci in ci_records:
                ch, _, ident = (ci.identity_value or "").partition(":")
                if ch in ("messenger", "instagram") and ident:
                    meta_identities.setdefault(ch, ident)
        except Exception:
            pass

        for meta_channel in ("messenger", "instagram"):
            if not reg.is_channel_available(project_id, meta_channel):
                results.append(_entry(meta_channel, False, "channel not configured"))
                continue
            recipient = meta_identities.get(meta_channel)
            if not recipient:
                results.append(_entry(meta_channel, False, "contact never messaged on this channel"))
                continue
            try:
                from app.models import MetaPageConnection
                from app.services.meta_window_service import MetaWindowService

                platform_filter = (
                    MetaPageConnection.messenger_enabled == True
                    if meta_channel == "messenger"
                    else MetaPageConnection.instagram_enabled == True
                )
                connection = self.db.query(MetaPageConnection).filter(
                    MetaPageConnection.project_id == project_id,
                    MetaPageConnection.is_active == True,
                    platform_filter,
                ).first()
                if not connection:
                    results.append(_entry(meta_channel, False, "channel not configured"))
                    continue
                window_open = MetaWindowService(self.db).is_window_open(
                    connection.id, meta_channel, recipient
                )
                if window_open:
                    results.append(_entry(meta_channel, True))
                else:
                    results.append(_entry(
                        meta_channel, True,
                        "24h window closed — human replies use the Human Agent tag",
                    ))
            except Exception:
                results.append(_entry(meta_channel, False, "channel check failed"))

        return results

    # ── Bulk operations ─────────────────────────────────────────────────

    def bulk_update_status(
        self,
        project_id: int,
        ticket_ids: List[int],
        status: str,
    ) -> int:
        """Bulk update status for multiple tickets. Returns count updated."""
        tickets = self.db.query(SupportTicket).filter(
            SupportTicket.project_id == project_id,
            SupportTicket.id.in_(ticket_ids),
        ).all()

        now = datetime.utcnow()
        count = 0
        for ticket in tickets:
            if ticket.status == status:
                continue
            ticket.status = status
            if status == "resolved" and not ticket.resolved_at:
                ticket.resolved_at = now
            elif status == "closed" and not ticket.closed_at:
                ticket.closed_at = now
            count += 1

        self.db.commit()

        # Agent-initiated tickets have no escalation origin. Release their
        # human route when a bulk action closes or resolves them.
        if status in ("resolved", "closed"):
            for ticket in tickets:
                self._clear_human_routing(ticket)

        return count

    # ── Block / Unblock contacts ──────────────────────────────────────────

    def block_contact(
        self,
        project_id: int,
        identifier: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Block a contact — sets is_blocked, closes open tickets, clears routing state."""
        from app.models.messaging import MessagingUser

        user = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.external_id == identifier,
        ).first()
        if not user:
            raise ValueError(f"Contact '{identifier}' not found in project {project_id}")

        user.is_blocked = True
        user.blocked_at = datetime.utcnow()
        user.blocked_reason = reason

        # Close any open tickets for this contact
        open_tickets = self.db.query(SupportTicket).filter(
            SupportTicket.project_id == project_id,
            SupportTicket.customer_identifier == identifier,
            SupportTicket.status.in_(["open", "in_progress", "waiting_customer"]),
        ).all()
        for ticket in open_tickets:
            ticket.status = "closed"
            ticket.closed_at = datetime.utcnow()
            self._insert_system_message(ticket, "contact_blocked", "Contact blocked by agent")
            self._publish(ticket.project_id, rt.TICKET_UPDATED, self._ticket_dict(ticket))

        # Clear routing states for this contact
        from app.models import ContactRoutingState
        self.db.query(ContactRoutingState).filter(
            ContactRoutingState.project_id == project_id,
            ContactRoutingState.contact_identifier == identifier,
        ).delete(synchronize_session="fetch")

        self.db.commit()

        return {
            "id": user.id,
            "external_id": user.external_id,
            "name": user.name,
            "phone": user.phone,
            "is_blocked": True,
            "blocked_at": user.blocked_at.isoformat() if user.blocked_at else None,
            "blocked_reason": user.blocked_reason,
        }

    def unblock_contact(self, project_id: int, identifier: str) -> Dict[str, Any]:
        """Unblock a contact."""
        from app.models.messaging import MessagingUser

        user = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.external_id == identifier,
        ).first()
        if not user:
            raise ValueError(f"Contact '{identifier}' not found in project {project_id}")

        user.is_blocked = False
        user.blocked_at = None
        user.blocked_reason = None
        self.db.commit()

        return {
            "id": user.id,
            "external_id": user.external_id,
            "name": user.name,
            "phone": user.phone,
            "is_blocked": False,
        }

    def get_blocked_contacts(
        self,
        project_id: int,
        page: int = 1,
        page_size: int = 50,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get paginated list of blocked contacts."""
        from app.models.messaging import MessagingUser

        query = self.db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.is_blocked == True,
        )

        if search:
            search_filter = f"%{search}%"
            query = query.filter(
                or_(
                    MessagingUser.name.ilike(search_filter),
                    MessagingUser.external_id.ilike(search_filter),
                    MessagingUser.phone.ilike(search_filter),
                    MessagingUser.email.ilike(search_filter),
                )
            )

        total = query.count()
        total_pages = max(1, (total + page_size - 1) // page_size)
        contacts = query.order_by(desc(MessagingUser.blocked_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return {
            "contacts": [
                {
                    "id": c.id,
                    "project_id": c.project_id,
                    "external_id": c.external_id,
                    "name": c.name,
                    "phone": c.phone,
                    "email": c.email,
                    "blocked_at": c.blocked_at.isoformat() if c.blocked_at else None,
                    "blocked_reason": c.blocked_reason,
                }
                for c in contacts
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }


def get_support_inbox_service(db: Session) -> SupportInboxService:
    """Factory function to get SupportInboxService instance."""
    return SupportInboxService(db)
