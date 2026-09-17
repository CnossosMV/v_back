"""
Inbound Router — core message routing engine.

Takes a canonical InboundMessage and:
1. Checks ContactRoutingState (priority-based ownership)
2. Evaluates InboxAssignmentRules
3. Falls back to instance-linkage (_find_responder)
4. Final fallback — routes to human inbox if project_id resolved
5. Dispatches to the correct handler
6. Emits lifecycle events (message_received, message_routed, message_replied)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import (
    AgentTeam, Chatbot, ChatMessage, ChatSession, CustomerSMTPConfig,
    EmailInboundAddress, InboxAssignmentRule,
    MessagingEvent, MessagingUser, SupportTicket, WhatsAppInstance,
)
from app.schemas.inbound_router import InboundMessage
from .routing_state_service import RoutingStateService, HANDLER_PRIORITIES

logger = logging.getLogger(__name__)


class InboundRouter:
    """Route normalised inbound messages to the correct handler."""

    def __init__(self, db: Session):
        self.db = db
        self.state_service = RoutingStateService(db)

    async def route(self, message: InboundMessage) -> Dict[str, Any]:
        """
        Main entry point.

        Returns a dict with at least {"success": bool}.
        """
        start_ms = time.time()

        # 1. Resolve project_id from instance if not yet set
        project_id = message.project_id
        instance = None

        if not project_id and message.instance_name:
            instance = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.instance_name == message.instance_name,
            ).first()
            if instance:
                message.instance_id = instance.id
                # Derive project_id from linked chatbot or agent team
                project_id = self._project_from_instance(instance)
                message.project_id = project_id

        if not project_id and message.instance_id:
            if message.channel == "email":
                smtp_cfg = self.db.query(CustomerSMTPConfig).filter(
                    CustomerSMTPConfig.id == message.instance_id,
                ).first()
                if smtp_cfg:
                    project_id = smtp_cfg.project_id
                    message.project_id = project_id
            else:
                instance = self.db.query(WhatsAppInstance).filter(
                    WhatsAppInstance.id == message.instance_id,
                ).first()
                if instance:
                    project_id = self._project_from_instance(instance)
                    message.project_id = project_id

        # 1b. Process media (audio transcription, image description, document extraction)
        if any(p.type in ("audio", "image", "video", "document") for p in message.content_pieces):
            try:
                from .media_processor import MediaProcessor, assemble_resolved_text
                processor = MediaProcessor(self.db)
                new_pieces = []
                for piece in message.content_pieces:
                    if piece.type in ("audio", "image", "video", "document"):
                        piece = await processor.process_content_piece(
                            piece, project_id or 0, instance,
                            provider_type=message.provider_type,
                            raw_payload=message.raw_payload,
                            instance_name=message.instance_name,
                        )
                    new_pieces.append(piece)
                message.content_pieces = new_pieces
                message.resolved_text = assemble_resolved_text(new_pieces)
            except Exception as e:
                logger.warning(f"Media processing error (continuing): {e}")

        # Resolve inbound contact — create MessagingUser if not exists
        resolved_contact = None
        contact_resolution_error = None
        if project_id and message.contact_identifier:
            try:
                from app.services.messaging.identity_resolver import identity_resolver
                resolved_contact = identity_resolver.resolve_inbound_contact(
                    self.db, project_id, message.channel or 'whatsapp',
                    message.contact_identifier,
                    message.instance_id,
                    push_name=message.push_name,
                )
                if resolved_contact is None:
                    contact_resolution_error = RuntimeError("contact_not_resolved")
            except Exception as e:
                contact_resolution_error = e
                logger.warning(f"Inbound contact resolution failed: {e}")

        # Compliance requests must never continue through automation when the
        # contact cannot be resolved. The retryable result lets webhook callers
        # ask the provider to redeliver, while the alert contains no identifier.
        if (
            project_id
            and contact_resolution_error is not None
            and (message.resolved_text or "").strip().lower() in self._STOP_KEYWORDS
        ):
            try:
                self.db.rollback()
                from app.services.campaigns.alerts import upsert_operational_alert
                upsert_operational_alert(
                    self.db,
                    project_id=project_id,
                    dedupe_key=f"inbound-compliance-resolution:{project_id}:{message.channel or 'unknown'}",
                    alert_type="inbound_compliance_resolution_failed",
                    title="Inbound opt-out could not resolve contact",
                    message="A STOP request was held before routing because contact resolution failed.",
                    severity="critical",
                    context={"channel": message.channel or "unknown", "retryable": True},
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                logger.exception("Could not persist inbound compliance resolution alert")
            return {"success": False, "retryable": True, "error": "contact_resolution_failed"}

        # 1c. Inbound message dedup (G15) — skip if same message_id within 5s
        if project_id and message.message_id:
            dedup_result = self._check_inbound_dedup(project_id, message.message_id)
            if dedup_result:
                logger.info(f"Duplicate inbound message {message.message_id} — skipping")
                return {"success": True, "deduplicated": True}

        # 1d. Opt-out check (G1, G11) — stop routing if contact has opted out
        if project_id:
            opt_out_result = self._check_opt_out(project_id, message, resolved_contact)
            if opt_out_result:
                return opt_out_result

        # 1e. Block check — silently drop messages from blocked contacts
        if project_id:
            blocked_result = self._check_blocked(project_id, message, resolved_contact)
            if blocked_result:
                return blocked_result

        # 1f. Reply attribution — stamp the most recent outbound send this
        # message plausibly answers (MES reach signal + channel.<ch>.replied)
        if project_id and resolved_contact:
            try:
                self._stamp_reply_on_send_log(project_id, resolved_contact, message)
            except Exception as e:
                logger.warning(f"Reply attribution failed (continuing): {e}")

        # 2. Emit message_received event
        event_props = {
            "channel": message.channel,
            "contact_identifier": message.contact_identifier,
            "message_id": message.message_id,
            "message_type": message.content_pieces[0].type if message.content_pieces else "unknown",
            "has_media": any(p.type in ("audio", "image", "video", "document") for p in message.content_pieces),
            "content_length": len(message.resolved_text or ""),
        }
        if message.channel == "email":
            if message.thread and message.thread.subject:
                event_props["subject"] = message.thread.subject
            if message.inbound_address_id:
                event_props["inbound_address_id"] = message.inbound_address_id
        self._emit_event(
            project_id=project_id,
            event_name="message_received",
            properties=event_props,
            user_id=resolved_contact.id if resolved_contact else None,
        )

        # 3. Check existing routing state(s) — multi-row aware
        routing_source = "fallback"
        handler_type = None
        handler_id = None
        all_states = []
        funnel_states = []

        logger.warning(f"[ROUTE] project_id={project_id} identifier={message.contact_identifier} channel={message.channel}")

        if project_id:
            all_states = self.state_service.get_all_states(
                project_id, message.contact_identifier, message.channel,
            )

            logger.warning(f"[ROUTE] Found {len(all_states)} routing states: {[(s.handler_type, s.handler_id, s.enrollment_id) for s in all_states]}")

            if all_states:
                primary_state = all_states[0]  # Highest priority
                handler_type = primary_state.handler_type
                handler_id = primary_state.handler_id
                routing_source = "state"

                # Collect funnel states for multi-enrollment routing
                funnel_states = [
                    s for s in all_states
                    if s.handler_type in ("funnel_wait", "funnel_send")
                ]

                # Check thread timeout on primary state
                if primary_state.session_id:
                    session = self.db.query(ChatSession).filter(
                        ChatSession.id == primary_state.session_id,
                    ).first()
                    if session and self._is_thread_expired(session):
                        logger.info(f"Thread expired for session {session.id}, clearing state")
                        self.state_service.clear_state(
                            project_id, message.contact_identifier, message.channel,
                            enrollment_id=primary_state.enrollment_id,
                        )
                        session.is_active = False
                        session.ended_at = datetime.utcnow()
                        self.db.commit()
                        # Re-fetch remaining states
                        all_states = self.state_service.get_all_states(
                            project_id, message.contact_identifier, message.channel,
                        )
                        funnel_states = [
                            s for s in all_states
                            if s.handler_type in ("funnel_wait", "funnel_send")
                        ]
                        if all_states:
                            primary_state = all_states[0]
                            handler_type = primary_state.handler_type
                            handler_id = primary_state.handler_id
                        else:
                            handler_type = None
                            handler_id = None
                            routing_source = "fallback"

        # 3b. Validate human routing state — clear if no active ticket (G2)
        if handler_type == "human" and project_id:
            primary_state = all_states[0] if all_states else None
            if primary_state and primary_state.session_id:
                active_ticket = self.db.query(SupportTicket).filter(
                    SupportTicket.session_id == primary_state.session_id,
                    SupportTicket.status.in_(["open", "in_progress", "waiting_customer"]),
                ).first()
                if not active_ticket:
                    logger.info(
                        f"Stale human routing state for {message.contact_identifier} "
                        f"(session {primary_state.session_id} has no active ticket). Clearing."
                    )
                    self.state_service.clear_state(
                        project_id, message.contact_identifier, message.channel,
                    )
                    # Re-evaluate from remaining states
                    all_states = self.state_service.get_all_states(
                        project_id, message.contact_identifier, message.channel,
                    )
                    funnel_states = [
                        s for s in all_states
                        if s.handler_type in ("funnel_wait", "funnel_send")
                    ]
                    if all_states:
                        primary_state = all_states[0]
                        handler_type = primary_state.handler_type
                        handler_id = primary_state.handler_id
                    else:
                        handler_type = None
                        handler_id = None
                        routing_source = "fallback"

        # 3c. Execute silent inbox rules (G3) — run actions without stopping routing
        if project_id:
            self._execute_silent_rules(project_id, message)

        # 4. If no active state, evaluate inbox assignment rules
        if not handler_type and project_id:
            rule_result = self._evaluate_inbox_rules(project_id, message)
            if rule_result:
                handler_type = rule_result["handler_type"]
                handler_id = rule_result["handler_id"]
                routing_source = "rule"

        # 5. If still nothing, fall back to instance-linkage
        if not handler_type:
            fallback = self._find_responder_from_instance(message)
            if fallback:
                handler_type = fallback["handler_type"]
                handler_id = fallback["handler_id"]
                routing_source = "fallback"

        # 6. Final fallback — route to human inbox
        if not handler_type and project_id:
            handler_type = "human"
            handler_id = None
            routing_source = "inbox_fallback"
            logger.info(f"[ROUTE] No handler found, routing to inbox fallback: {message.contact_identifier}")

        if not handler_type:
            logger.warning(f"No handler found for {message.contact_identifier} on {message.channel}")
            return {"success": False, "error": "No handler configured"}

        # 7. Emit message_routed event
        self._emit_event(
            project_id=project_id,
            event_name="message_routed",
            properties={
                "handler_type": handler_type,
                "handler_id": handler_id,
                "routing_source": routing_source,
                "channel": message.channel,
                "contact_identifier": message.contact_identifier,
            },
        )

        # 8. Dispatch to handler
        contact_id = resolved_contact.id if resolved_contact else None
        result = await self._dispatch(
            message=message,
            handler_type=handler_type,
            handler_id=handler_id,
            project_id=project_id,
            funnel_states=funnel_states,
            contact_id=contact_id,
        )

        # 9. Update routing state (skip for funnel types — they manage their own state)
        if project_id and handler_type != "idle" and handler_type not in ("funnel_wait", "funnel_send"):
            session_id = result.get("session_id")
            self.state_service.set_handler(
                project_id=project_id,
                identifier=message.contact_identifier,
                channel=message.channel,
                handler_type=handler_type,
                handler_id=handler_id,
                session_id=session_id,
            )

            # Update session last_inbound_at + inbound channel context
            if session_id:
                session = self.db.query(ChatSession).filter(ChatSession.id == session_id).first()
                if session:
                    session.last_inbound_at = datetime.utcnow()
                    session.handler_type = handler_type
                    # Store inbound channel context for reply routing
                    meta = session.session_metadata or {}
                    meta["inbound_instance_id"] = message.instance_id
                    meta["inbound_provider_id"] = getattr(message, "provider_id", None)
                    meta["inbound_provider_type"] = message.provider_type
                    meta["remote_jid"] = message.remote_jid
                    meta["remote_jid_alt"] = message.remote_jid_alt
                    meta["customer_phone"] = message.customer_phone
                    session.session_metadata = meta
                    self.db.commit()

        # 9b. Store inbound channel context in routing_metadata (ALL handler types)
        if project_id and handler_type != "idle":
            inbound_ctx = {
                "inbound_instance_id": message.instance_id,
                "inbound_provider_id": getattr(message, "provider_id", None),
                "inbound_provider_type": message.provider_type,
                "remote_jid": message.remote_jid,
                "remote_jid_alt": message.remote_jid_alt,
                "customer_phone": message.customer_phone,
            }
            try:
                self.state_service.update_inbound_context(
                    project_id=project_id,
                    identifier=message.contact_identifier,
                    channel=message.channel,
                    inbound_context=inbound_ctx,
                )
            except Exception as e:
                logger.warning(f"Failed to update inbound context: {e}")

        # 10. Emit message_replied event
        elapsed_ms = int((time.time() - start_ms) * 1000)
        self._emit_event(
            project_id=project_id,
            event_name="message_replied",
            properties={
                "handler_type": handler_type,
                "response_length": len(result.get("response", "") or ""),
                "response_time_ms": elapsed_ms,
                "success": result.get("success", False),
            },
        )

        return result

    # ── Internal helpers ─────────────────────────────────────────────────

    def _project_from_instance(self, instance: WhatsAppInstance) -> Optional[int]:
        """Deterministic project resolution for a WhatsApp instance.

        1. instance.project_id is authoritative.
        2. Legacy fallback (NULL project_id rows only): handler link, then
           single-active-project workspace — each hit is logged.
        3. Ambiguous → None. The message is NOT routed; never guess between
           projects (wrong-tenant routing is worse than an unrouted message).
        """
        if instance.project_id:
            return instance.project_id

        # Legacy fallback for rows that predate project scoping
        from app.services.handler_channel_link_service import HandlerChannelLinkService
        link_svc = HandlerChannelLinkService(self.db)
        handler = link_svc.find_handler_by_instance("whatsapp", instance.id)
        if handler:
            handler_project_id = None
            if handler["handler_type"] == "chatbot":
                chatbot = self.db.query(Chatbot).filter(Chatbot.id == handler["handler_id"]).first()
                if chatbot:
                    handler_project_id = chatbot.project_id
            elif handler["handler_type"] == "agent_team":
                team = self.db.query(AgentTeam).filter(AgentTeam.id == handler["handler_id"]).first()
                if team:
                    handler_project_id = team.project_id
            if handler_project_id:
                logger.warning(
                    f"LEGACY project inference for instance {instance.id} -> project "
                    f"{handler_project_id} (handler_channel_link)"
                )
                return handler_project_id

        if instance.workspace_id:
            from app.models import Project
            projects = self.db.query(Project).filter(
                Project.workspace_id == instance.workspace_id,
                Project.is_active == True,
            ).all()
            if len(projects) == 1:
                logger.warning(
                    f"LEGACY project inference for instance {instance.id} -> project "
                    f"{projects[0].id} (single project in workspace)"
                )
                return projects[0].id

        logger.error(
            f"Cannot resolve project for WhatsApp instance {instance.id} "
            f"(project_id NULL, no handler link, ambiguous workspace) — message NOT routed. "
            f"Assign the instance to a project to fix."
        )
        return None

    def _is_thread_expired(self, session: ChatSession) -> bool:
        """Check if the session's thread has timed out.

        Sessions with active human takeover tickets are never expired — the
        human agent controls when the conversation ends.
        """
        if getattr(session, 'human_takeover', False):
            # Verify there's still an active support ticket for this session
            from app.models import SupportTicket
            active_ticket = self.db.query(SupportTicket.id).filter(
                SupportTicket.session_id == session.id,
                SupportTicket.status.in_(["open", "in_progress", "waiting_customer"]),
            ).first()
            if active_ticket:
                return False

        timeout_minutes = session.thread_timeout_minutes or 30  # default 30 min
        last = session.last_inbound_at or session.last_interaction_at
        if not last:
            return False
        return datetime.utcnow() > last + timedelta(minutes=timeout_minutes)

    def _evaluate_inbox_rules(
        self, project_id: int, message: InboundMessage,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate InboxAssignmentRules in priority order. First match wins."""
        rules = (
            self.db.query(InboxAssignmentRule)
            .filter(
                InboxAssignmentRule.project_id == project_id,
                InboxAssignmentRule.is_active == True,
            )
            .order_by(InboxAssignmentRule.priority.desc())
            .all()
        )

        if not rules:
            return None

        from app.services.event_actions.conditions import ConditionEvaluator

        evaluator = ConditionEvaluator()
        event_data = {
            "channel": message.channel,
            "provider_type": message.provider_type,
            "message_type": message.content_pieces[0].type if message.content_pieces else "text",
            "body": message.resolved_text or "",
            "push_name": message.push_name or "",
            "contact_identifier": message.contact_identifier,
        }

        for rule in rules:
            # Skip silent rules — they are executed separately in step 3c
            if rule.is_silent:
                continue

            conditions = rule.conditions or []
            if not conditions:
                # No conditions = always matches
                return {
                    "handler_type": rule.destination_type,
                    "handler_id": rule.destination_id,
                }

            if evaluator.evaluate_all(conditions, event_data, match_mode=rule.match_mode):
                logger.info(f"Inbox rule '{rule.name}' matched for {message.contact_identifier}")
                return {
                    "handler_type": rule.destination_type,
                    "handler_id": rule.destination_id,
                }

        return None

    def _execute_silent_rules(self, project_id: int, message: InboundMessage) -> None:
        """Execute silent inbox rules — actions fire without stopping the routing chain (G3).

        Silent rules can tag contacts, update properties, fire webhooks, etc.
        They never assign a handler — they just perform side-effects.
        """
        rules = (
            self.db.query(InboxAssignmentRule)
            .filter(
                InboxAssignmentRule.project_id == project_id,
                InboxAssignmentRule.is_active == True,
                InboxAssignmentRule.is_silent == True,
            )
            .order_by(InboxAssignmentRule.priority.desc())
            .all()
        )

        if not rules:
            return

        from app.services.event_actions.conditions import ConditionEvaluator

        evaluator = ConditionEvaluator()
        event_data = {
            "channel": message.channel,
            "provider_type": message.provider_type,
            "message_type": message.content_pieces[0].type if message.content_pieces else "text",
            "body": message.resolved_text or "",
            "push_name": message.push_name or "",
            "contact_identifier": message.contact_identifier,
        }

        for rule in rules:
            conditions = rule.conditions or []
            matched = not conditions  # No conditions = always match
            if not matched:
                matched = evaluator.evaluate_all(conditions, event_data, match_mode=rule.match_mode)

            if matched and rule.silent_actions:
                logger.info(f"Silent rule '{rule.name}' matched for {message.contact_identifier}")
                for action in rule.silent_actions:
                    try:
                        self._execute_silent_action(project_id, message, action)
                    except Exception as e:
                        logger.warning(f"Silent action failed in rule '{rule.name}': {e}")

    def _execute_silent_action(
        self, project_id: int, message: InboundMessage, action: Dict[str, Any],
    ) -> None:
        """Execute a single silent action (tag, update property, webhook)."""
        action_type = action.get("action_type")
        config = action.get("config", {})

        if action_type == "add_tag":
            tag = config.get("tag")
            if tag:
                from app.models.messaging import MessagingUser
                user = self.db.query(MessagingUser).filter(
                    MessagingUser.project_id == project_id,
                    MessagingUser.external_id == message.contact_identifier,
                ).first()
                if user:
                    tags = list(user.tags or [])
                    if tag not in tags:
                        tags.append(tag)
                        user.tags = tags
                        self.db.commit()

        elif action_type == "update_property":
            key = config.get("key")
            value = config.get("value")
            if key:
                from app.models.messaging import MessagingUser
                from sqlalchemy.orm.attributes import flag_modified
                user = self.db.query(MessagingUser).filter(
                    MessagingUser.project_id == project_id,
                    MessagingUser.external_id == message.contact_identifier,
                ).first()
                if user:
                    props = dict(user.properties or {})
                    props[key] = value
                    user.properties = props
                    flag_modified(user, "properties")
                    self.db.commit()

        elif action_type == "webhook":
            url = config.get("url")
            if url:
                import httpx
                try:
                    httpx.post(url, json={
                        "project_id": project_id,
                        "contact_identifier": message.contact_identifier,
                        "channel": message.channel,
                        "message": (message.resolved_text or "")[:500],
                        "rule_action": action,
                    }, timeout=5)
                except Exception as e:
                    logger.warning(f"Silent webhook failed: {e}")

        else:
            logger.debug(f"Unknown silent action type: {action_type}")

    def _find_responder_from_instance(self, message: InboundMessage) -> Optional[Dict[str, Any]]:
        """Find chatbot or agent team from channel instance linkage (handler_channel_links).

        Works for any channel: looks up handler_channel_links by (channel, instance_id).
        For email, checks address-level routing first.
        For WhatsApp, also resolves instance_name → instance_id and checks legacy FKs.
        """
        # Email address-level routing (before handler_channel_links)
        if message.channel == "email" and message.inbound_address_id:
            addr = self.db.query(EmailInboundAddress).filter(
                EmailInboundAddress.id == message.inbound_address_id,
            ).first()
            if addr and addr.default_handler_type and addr.default_handler_id:
                if addr.default_handler_type == "chatbot":
                    chatbot = self.db.query(Chatbot).filter(
                        Chatbot.id == addr.default_handler_id, Chatbot.status == "active"
                    ).first()
                    if chatbot:
                        return {"handler_type": "chatbot", "handler_id": chatbot.id}
                elif addr.default_handler_type == "agent_team":
                    team = self.db.query(AgentTeam).filter(
                        AgentTeam.id == addr.default_handler_id, AgentTeam.status == "active"
                    ).first()
                    if team:
                        return {"handler_type": "agent_team", "handler_id": team.id}

        from app.services.handler_channel_link_service import HandlerChannelLinkService
        link_svc = HandlerChannelLinkService(self.db)

        # --- Generic path: use instance_id if set ---
        instance_id = message.instance_id

        # WhatsApp special case: resolve instance_name → instance_id if needed
        if not instance_id and message.instance_name and message.channel == "whatsapp":
            instance = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.instance_name == message.instance_name,
            ).first()
            if instance:
                instance_id = instance.id

        if not instance_id:
            return None

        # Channel-generic handler lookup via handler_channel_links
        handlers = link_svc.find_handlers_by_instance(message.channel, instance_id)

        # Priority 1: chatbot links
        for h in handlers:
            if h["handler_type"] == "chatbot":
                chatbot = self.db.query(Chatbot).filter(
                    Chatbot.id == h["handler_id"], Chatbot.status == "active"
                ).first()
                if chatbot:
                    return {"handler_type": "chatbot", "handler_id": chatbot.id}

        # Priority 2: agent team links
        for h in handlers:
            if h["handler_type"] == "agent_team":
                team = self.db.query(AgentTeam).filter(
                    AgentTeam.id == h["handler_id"], AgentTeam.status == "active"
                ).first()
                if team:
                    return {"handler_type": "agent_team", "handler_id": team.id}

        # Legacy fallback: direct FK on chatbot/agent_team (WhatsApp only)
        if message.channel == "whatsapp":
            chatbot = self.db.query(Chatbot).filter(
                Chatbot.whatsapp_instance_id == instance_id,
                Chatbot.status == "active",
            ).first()
            if chatbot:
                return {"handler_type": "chatbot", "handler_id": chatbot.id}

            team = self.db.query(AgentTeam).filter(
                AgentTeam.whatsapp_instance_id == instance_id,
                AgentTeam.status == "active",
            ).first()
            if team:
                return {"handler_type": "agent_team", "handler_id": team.id}

        return None

    async def _dispatch(
        self,
        message: InboundMessage,
        handler_type: str,
        handler_id: Optional[int],
        project_id: Optional[int],
        funnel_states: Optional[list] = None,
        contact_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Dispatch the message to the resolved handler."""
        from app.services.unified_chat_service import UnifiedChatService

        chat_service = UnifiedChatService(self.db)

        # Build legacy message_data dict for compatibility with existing methods
        message_data = {
            "message_id": message.message_id,
            "from": message.contact_identifier,
            "customer_phone": message.customer_phone,
            "body": message.resolved_text or "",
            "message_type": message.content_pieces[0].type if message.content_pieces else "text",
            "push_name": message.push_name,
            "remote_jid": message.remote_jid,
            "remote_jid_alt": message.remote_jid_alt,
            "key": (message.raw_payload or {}).get("key"),
            "raw": message.raw_payload,
            "content_pieces": [p.model_dump() for p in message.content_pieces],
        }

        if handler_type == "funnel_wait":
            return await self._handle_funnel_wait(message, handler_id, funnel_states=funnel_states or [])

        if handler_type == "funnel_send":
            logger.warning(f"[DISPATCH] funnel_send handler_id={handler_id} project_id={project_id}")
            return await self._handle_funnel_send(message, handler_id, project_id, funnel_states=funnel_states or [], contact_id=contact_id)

        if handler_type == "human":
            return await self._route_to_human_inbox(message, project_id, contact_id=contact_id)

        if handler_type == "agent_team":
            team = self.db.query(AgentTeam).filter(AgentTeam.id == handler_id).first()
            if not team:
                return {"success": False, "error": f"Agent team {handler_id} not found"}
            return await chat_service._process_agent_team_message(
                team=team,
                provider=None,
                customer_identifier=message.contact_identifier,
                channel=message.channel,
                message_data=message_data,
            )

        if handler_type == "chatbot":
            # Use the existing chatbot flow via process_incoming_message.
            # When the router already resolved the chatbot, pass it explicitly —
            # _find_responder's instance lookup is WhatsApp-only, so non-WhatsApp
            # channels (messenger/instagram/...) depend on this override.
            chatbot_override = None
            if handler_id:
                chatbot_override = self.db.query(Chatbot).filter(
                    Chatbot.id == handler_id
                ).first()
            return await chat_service.process_incoming_message(
                channel=message.channel,
                provider_type=message.provider_type,
                message_data=message_data,
                instance_name=message.instance_name,
                chatbot_override=chatbot_override,
            )

        return {"success": False, "error": f"Unknown handler type: {handler_type}"}

    # ── Human inbox helper ──────────────────────────────────────────────

    async def _route_to_human_inbox(
        self,
        message: InboundMessage,
        project_id: int,
        escalation_origin: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
        contact_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Route a message directly to the human support inbox.

        Creates a session (anchored to any active project chatbot) and a
        support ticket without requiring a chatbot/agent-team responder.
        """
        from app.services.support_inbox_service import SupportInboxService

        # 1. Find any active chatbot in the project (session anchor)
        chatbot = self.db.query(Chatbot).filter(
            Chatbot.project_id == project_id,
            Chatbot.status == "active",
        ).first()

        if not chatbot:
            logger.error(
                f"[HUMAN_INBOX] No active chatbot in project {project_id} "
                "to anchor session"
            )
            return {
                "success": False,
                "error": "No active chatbot in project to create support session",
            }

        # 2. Get or create a ChatSession with handler_type="human"
        session = self.db.query(ChatSession).filter(
            ChatSession.user_identifier == message.contact_identifier,
            ChatSession.chatbot_id == chatbot.id,
            ChatSession.is_active == True,
            ChatSession.handler_type == "human",
        ).first()

        if not session:
            session = ChatSession(
                chatbot_id=chatbot.id,
                user_identifier=message.contact_identifier,
                channel=message.channel,
                is_active=True,
                handler_type="human",
                contact_id=contact_id,
            )
            self.db.add(session)
            self.db.flush()
        elif contact_id and not session.contact_id:
            session.contact_id = contact_id

        # 3. Store the inbound message
        chat_msg = ChatMessage(
            session_id=session.id,
            role="user",
            content=message.resolved_text or "",
            sender_type="customer",
            content_pieces=[p.model_dump() for p in message.content_pieces] if message.content_pieces else None,
            resolved_text=message.resolved_text,
            channel=message.channel,
        )
        self.db.add(chat_msg)
        self.db.commit()

        # 4. Create support ticket (returns existing if one is already open)
        inbox_svc = SupportInboxService(self.db)

        # Track whether a ticket already exists so we can detect follow-ups
        existing_ticket = self.db.query(SupportTicket).filter(
            SupportTicket.session_id == session.id,
            SupportTicket.status.in_(["open", "in_progress", "waiting_customer"]),
        ).first()
        is_followup = existing_ticket is not None

        ticket = inbox_svc.create_ticket(
            session_id=session.id,
            reason=reason or "Human handoff from inbound message",
            escalation_origin=escalation_origin,
        )

        # 5. Enrich ticket with WhatsApp routing metadata (so agent replies
        #    can be sent back through the correct instance/number)
        changed = False
        if message.customer_phone and not ticket.customer_phone:
            ticket.customer_phone = message.customer_phone
            changed = True
        if not ticket.customer_phone and message.contact_identifier:
            ticket.customer_phone = message.contact_identifier
            changed = True
        if message.push_name and not ticket.customer_name:
            ticket.customer_name = message.push_name
            changed = True

        meta = ticket.ticket_metadata or {}
        if message.remote_jid and not meta.get("remote_jid"):
            meta["remote_jid"] = message.remote_jid
            changed = True
        if message.remote_jid_alt and not meta.get("remote_jid_alt"):
            meta["remote_jid_alt"] = message.remote_jid_alt
            changed = True
        if message.instance_id and not meta.get("instance_id"):
            meta["instance_id"] = message.instance_id
            changed = True
        if message.provider_type and not meta.get("provider_type"):
            meta["provider_type"] = message.provider_type
            changed = True
        if getattr(message, "provider_id", None) and not meta.get("provider_id"):
            meta["provider_id"] = message.provider_id
            changed = True
        # Enrich contact_id on ticket and session
        if contact_id and not ticket.contact_id:
            ticket.contact_id = contact_id
            changed = True
        if contact_id and not session.contact_id:
            session.contact_id = contact_id
            changed = True
        # Enrich customer details from MessagingUser when not set by message
        if contact_id and (not ticket.customer_name or not ticket.customer_phone):
            contact = self.db.query(MessagingUser).filter(MessagingUser.id == contact_id).first()
            if contact:
                if not ticket.customer_name and contact.name:
                    ticket.customer_name = contact.name
                    changed = True
                if not ticket.customer_phone and contact.phone:
                    ticket.customer_phone = contact.phone
                    changed = True
        if changed:
            ticket.ticket_metadata = meta
            self.db.commit()

        # 6. Notify inbox UI of new messages (both new tickets and follow-ups)
        from app.services.realtime.redis_pubsub import publish_inbox_event
        from app.services.realtime import event_types as rt
        import asyncio

        event_data = {
            "ticket_id": ticket.id,
            "ticket_number": ticket.ticket_number,
            "message_id": chat_msg.id,
            "content": chat_msg.content,
            "content_pieces": chat_msg.content_pieces,
            "sender_type": "customer",
            "channel": chat_msg.channel or message.channel,
            "timestamp": chat_msg.timestamp.isoformat() if chat_msg.timestamp else None,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                publish_inbox_event(project_id, rt.MESSAGE_RECEIVED, event_data)
            )
        except RuntimeError:
            pass  # No running loop

        # 7. Push notification for follow-up customer messages
        if is_followup:
            try:
                from app.services.push_notification_service import PushNotificationService
                push_svc = PushNotificationService(self.db)
                push_title = f"New message — #{ticket.ticket_number}"
                push_body = (chat_msg.content or "")[:120]
                push_data = {
                    "url": f"/support/{ticket.id}",
                    "ticket_id": str(ticket.id),
                    "project_id": str(project_id),
                }
                if ticket.assigned_to_user_id:
                    push_svc.send_notification(ticket.assigned_to_user_id, push_title, push_body, push_data)
                else:
                    push_svc.send_to_project_agents(project_id, push_title, push_body, push_data)
            except Exception as e:
                logger.warning(f"[HUMAN_INBOX] Push notification failed: {e}")

        logger.info(
            f"[HUMAN_INBOX] Routed to inbox: project={project_id} "
            f"contact={message.contact_identifier} ticket={ticket.ticket_number}"
            f" followup={is_followup}"
        )

        return {
            "success": True,
            "handler": "human_inbox",
            "session_id": session.id,
            "ticket_id": ticket.id,
            "ticket_number": ticket.ticket_number,
        }

    async def _handle_funnel_wait(
        self, message: InboundMessage, enrollment_id: Optional[int],
        funnel_states: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Forward inbound message to funnel engine for wait-for-reply step.

        If multiple funnel states exist (concurrent funnels), classify against all
        enrollments' goals.  If exactly one matches, route there.  If ambiguous
        (multiple match), route to inbox.

        If the step has goals[] configured, run MilestoneClassifier first and
        route via handle_classified_reply.  Otherwise, fall through to the
        existing handle_inbound_reply path.
        """
        if not enrollment_id:
            return {"success": False, "error": "No enrollment_id for funnel_wait"}

        try:
            from app.services.funnel_engine import FunnelEngine
            from app.models import FunnelEnrollment, FunnelStep, FunnelEnrollmentThread

            engine = FunnelEngine(self.db)

            # Multi-enrollment classification: if multiple funnel_wait states exist
            wait_states = [
                s for s in (funnel_states or [])
                if s.handler_type == "funnel_wait"
            ]
            if len(wait_states) > 1:
                result = await self._classify_multi_enrollment(
                    message, wait_states, "wait_for_reply", engine,
                )
                if result:
                    return result
                # If _classify_multi_enrollment returned None, fall through
                # (means no goals on any enrollment, use primary)

            # Single enrollment path (or multi with no goals)
            enrollment = self.db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == enrollment_id,
                FunnelEnrollment.status == "active",
            ).first()
            if not enrollment:
                return {"success": False, "error": "Enrollment not found or inactive"}

            step = enrollment.current_step
            thread = None
            if not step or step.step_type != "wait_for_reply":
                thread = (
                    self.db.query(FunnelEnrollmentThread)
                    .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
                    .filter(
                        FunnelEnrollmentThread.enrollment_id == enrollment.id,
                        FunnelEnrollmentThread.status == "active",
                        FunnelStep.step_type == "wait_for_reply",
                    )
                    .first()
                )
                if thread:
                    step = thread.current_step

            config = (step.step_config if step else None) or {}
            goals = config.get("goals", [])

            if goals:
                # Debounce check
                debounce_result = self._check_debounce(
                    enrollment, config, message,
                )
                if debounce_result:
                    return {"success": True, "funnel_result": debounce_result}

                # Classify
                from app.services.funnel.milestone_classifier import MilestoneClassifier
                classifier = MilestoneClassifier(self.db)
                classification = classifier.classify(
                    message_text=message.resolved_text or "",
                    goals=goals,
                    project_id=enrollment.funnel.project_id,
                    content_pieces=[p.model_dump() for p in message.content_pieces],
                    conversation_history=self._get_conversation_history(
                        enrollment, config.get("ai_context_messages", 10),
                        project_id=enrollment.funnel.project_id,
                    ),
                )

                # Record classification timestamp for debounce
                self._mark_classified(enrollment)

                result = engine.handle_classified_reply(
                    enrollment_id=enrollment.id,
                    message=message,
                    classification=classification,
                )

                # If on_topic — delegate to handler for this single message
                if result.get("delegate"):
                    delegate_result = await self._delegate_to_handler(
                        message, result["delegate"], enrollment, config,
                    )
                    return {"success": True, "funnel_result": result, "delegate_result": delegate_result}

                return {"success": True, "funnel_result": result}

            # No goals — existing path
            result = engine.handle_inbound_reply(enrollment_id, message)
            return {"success": True, "funnel_result": result}
        except Exception as e:
            logger.error(f"Error handling funnel wait reply: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _funnel_handoff_send(
        self,
        *,
        mode: str,
        instance,
        to_number: str,
        project_id: Optional[int],
        source_type: str,
        source_id: Optional[int],
        instance_id: Optional[int],
        user_id: Optional[int] = None,
        text: Optional[str] = None,
        media_url: Optional[str] = None,
        media_type: Optional[str] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> None:
        """0D reroute helper for funnel-handoff WhatsApp replies.

        mode 'off'     -> legacy WhatsAppSender only (current behavior).
        mode 'shadow'  -> legacy send + a dry_run send() parity log (no 2nd send).
        mode 'enforce' -> SendService.send() is authoritative; legacy skipped.

        Routing through send() is what gives these replies a SendLog + (with
        SEND_LEDGER_IN_SEND) a contact_ledger row — closing the bypass.
        """
        from app.services.channels.base import OutboundContent
        from app.services.channels.send_service import SendService

        def _content() -> OutboundContent:
            if media_url:
                return OutboundContent(
                    content_type="media", media_url=media_url,
                    media_type=media_type, text=caption,
                )
            return OutboundContent(content_type="text", text=text)

        async def _via_send(dry: bool):
            return await SendService(self.db).send(
                project_id=project_id,
                user_id=user_id,
                recipient=to_number,
                content=_content(),
                channel="whatsapp",
                source_type=source_type,
                source_id=source_id,
                instance_config={"instance_id": instance_id} if instance_id else None,
                dry_run=dry,
            )

        if mode == "enforce":
            await _via_send(dry=False)
            return

        # off / shadow: legacy direct send stays authoritative
        from app.services.whatsapp_sender import WhatsAppSender
        sender = WhatsAppSender(self.db)
        if media_url:
            await sender.send_media_message(
                instance=instance, to_number=to_number, media_url=media_url,
                media_type=media_type, caption=caption, filename=filename,
            )
        else:
            await sender.send_message(
                instance=instance, to_number=to_number, message=text,
            )

        if mode == "shadow":
            try:
                decision = await _via_send(dry=True)
                logger.info(
                    "[reroute][shadow] %s legacy=whatsapp would_status=%s would_channel=%s",
                    source_type, decision.status, decision.channel_used,
                )
            except Exception as e:
                logger.warning(
                    "[reroute][shadow] parity check failed for %s: %s", source_type, e,
                )

    async def _handle_funnel_send(
        self, message: InboundMessage, enrollment_id: Optional[int], project_id: Optional[int],
        funnel_states: Optional[list] = None, contact_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Forward inbound message to the configured handoff handler for a send_message step.

        The routing state metadata contains: step_id, handoff_type, handoff_id.
        If the step has goals[], classify first — goal_met completes handoff,
        blocker/off_topic pauses enrollment.  Otherwise delegate to handoff handler.
        """
        if not enrollment_id:
            return {"success": False, "error": "No enrollment_id for funnel_send"}

        try:
            from app.models import FunnelEnrollment, FunnelStep

            # Multi-enrollment classification: if multiple funnel_send states
            wa_states = [
                s for s in (funnel_states or [])
                if s.handler_type == "funnel_send"
            ]
            if len(wa_states) > 1:
                from app.services.funnel_engine import FunnelEngine
                engine = FunnelEngine(self.db)
                result = await self._classify_multi_enrollment(
                    message, wa_states, "send_message", engine,
                )
                if result:
                    return result

            enrollment = self.db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == enrollment_id,
            ).first()
            if not enrollment:
                return {"success": False, "error": f"Enrollment {enrollment_id} not found"}

            # Log inbound reply in funnel enrollment logs
            from app.models import FunnelEnrollmentLog
            log_entry = FunnelEnrollmentLog(
                enrollment_id=enrollment.id,
                step_id=enrollment.current_step_id,
                action="whatsapp_reply_received",
                details={
                    "from": message.contact_identifier,
                    "message": (message.resolved_text or "")[:500],
                },
            )
            self.db.add(log_entry)
            self.db.commit()

            # Get routing state metadata for handoff config — find the specific funnel row
            state = None
            for s in (funnel_states or []):
                if s.enrollment_id == enrollment_id:
                    state = s
                    break
            if not state:
                state = self.state_service.get_state(
                    project_id=project_id or enrollment.funnel.project_id,
                    identifier=message.contact_identifier,
                    channel=message.channel,
                )
            routing_meta = (state.routing_metadata if state else None) or {}
            handoff_type = routing_meta.get("handoff_type", "chatbot")
            handoff_id = routing_meta.get("handoff_id")
            step_id = routing_meta.get("step_id")
            logger.warning(f"[FUNNEL_WA] enrollment={enrollment_id} handoff_type={handoff_type} handoff_id={handoff_id} step_id={step_id} state_found={state is not None}")

            # Load step to check for goals
            step = None
            if step_id:
                step = self.db.query(FunnelStep).filter(FunnelStep.id == step_id).first()
            config = (step.step_config if step else None) or {}
            goals = config.get("goals", [])

            from app.services.unified_chat_service import UnifiedChatService
            chat_service = UnifiedChatService(self.db)

            message_data = {
                "message_id": message.message_id,
                "from": message.contact_identifier,
                "customer_phone": message.customer_phone,
                "body": message.resolved_text or "",
                "message_type": message.content_pieces[0].type if message.content_pieces else "text",
                "push_name": message.push_name,
                "remote_jid": message.remote_jid,
                "remote_jid_alt": message.remote_jid_alt,
                "key": (message.raw_payload or {}).get("key"),
                "raw": message.raw_payload,
                "content_pieces": [p.model_dump() for p in message.content_pieces],
            }

            if goals:
                # Classify before dispatch
                from app.services.funnel.milestone_classifier import MilestoneClassifier
                from app.services.funnel_engine import FunnelEngine
                classifier = MilestoneClassifier(self.db)
                classification = classifier.classify(
                    message_text=message.resolved_text or "",
                    goals=goals,
                    project_id=enrollment.funnel.project_id,
                    content_pieces=[p.model_dump() for p in message.content_pieces],
                    conversation_history=self._get_conversation_history(
                        enrollment, config.get("ai_context_messages", 10),
                        project_id=enrollment.funnel.project_id,
                    ),
                )

                engine = FunnelEngine(self.db)

                if classification.outcome == "goal_met":
                    result = engine.complete_send_handoff(enrollment.id, step_id=step_id)
                    return {"success": True, "funnel_result": result, "classification": "goal_met"}

                if classification.outcome in ("blocker", "off_topic"):
                    result = engine.handle_classified_reply(
                        enrollment_id=enrollment.id,
                        message=message,
                        classification=classification,
                    )
                    return {"success": True, "funnel_result": result}

                # on_topic — delegate to handoff handler (don't change routing state)
                # Inject goal context into message_data for the handler
                goal_descriptions = [g.get("description", "") for g in goals if g.get("description")]
                if goal_descriptions:
                    message_data["_funnel_goal_context"] = (
                        "The user is in a funnel step. Goals: " + "; ".join(goal_descriptions)
                        + ". Help guide them toward completing a goal."
                    )

            # Standard handoff delegation (no goals, or on_topic)
            if handoff_type == "agent_team" and handoff_id:
                team = self.db.query(AgentTeam).filter(AgentTeam.id == handoff_id).first()
                if team:
                    result = await chat_service._process_agent_team_message(
                        team=team,
                        provider=None,
                        customer_identifier=message.contact_identifier,
                        channel=message.channel,
                        message_data=message_data,
                    )

                    # Fallback: if team has no WhatsApp instance, send via funnel's instance
                    response_text = result.get("response")
                    instance_id = routing_meta.get("instance_id")
                    from app.services.handler_channel_link_service import HandlerChannelLinkService
                    _link_svc = HandlerChannelLinkService(self.db)
                    _team_wa_link = _link_svc.get_link("agent_team", team.id, "whatsapp")
                    if response_text and instance_id and not _team_wa_link and not team.whatsapp_instance_id:
                        from app.models import WhatsAppInstance
                        instance = self.db.query(WhatsAppInstance).filter(
                            WhatsAppInstance.id == instance_id,
                            (WhatsAppInstance.project_id == team.project_id)
                            | (WhatsAppInstance.project_id.is_(None)),
                        ).first()
                        if not instance:
                            logger.error(
                                f"Funnel handoff send blocked: instance {instance_id} "
                                f"does not belong to project {team.project_id}"
                            )
                        if instance:
                            from app.services.channels.consolidation import reroute_mode
                            await self._funnel_handoff_send(
                                mode=reroute_mode("agent_team_funnel", self.db, project_id),
                                instance=instance,
                                to_number=message.contact_identifier,
                                project_id=team.project_id,
                                source_type="agent_team",
                                source_id=team.id,
                                instance_id=instance_id,
                                user_id=contact_id,
                                text=response_text,
                            )

                    return result
                return {"success": False, "error": f"Agent team {handoff_id} not found"}

            if handoff_type == "chatbot" and handoff_id:
                chatbot = self.db.query(Chatbot).filter(Chatbot.id == handoff_id).first()
                if chatbot:
                    # Generate response only — router handles sending via the funnel's instance
                    result = await chat_service.process_incoming_message(
                        channel=message.channel,
                        provider_type=message.provider_type,
                        message_data=message_data,
                        instance_name=message.instance_name,
                        chatbot_override=chatbot,
                        skip_send=True,
                    )

                    # Send the response via the funnel's WhatsApp instance
                    response_text = result.get("response")
                    instance_id = routing_meta.get("instance_id")
                    if response_text and instance_id:
                        from app.models import WhatsAppInstance
                        instance = self.db.query(WhatsAppInstance).filter(
                            WhatsAppInstance.id == instance_id,
                            (WhatsAppInstance.project_id == chatbot.project_id)
                            | (WhatsAppInstance.project_id.is_(None)),
                        ).first()
                        if not instance:
                            logger.error(
                                f"Funnel handoff send blocked: instance {instance_id} "
                                f"does not belong to project {chatbot.project_id}"
                            )
                        if instance:
                            from app.services.channels.consolidation import reroute_mode
                            _cb_mode = reroute_mode("chatbot_funnel", self.db, project_id)
                            await self._funnel_handoff_send(
                                mode=_cb_mode,
                                instance=instance,
                                to_number=message.contact_identifier,
                                project_id=chatbot.project_id,
                                source_type="chatbot",
                                source_id=chatbot.id,
                                instance_id=instance_id,
                                user_id=contact_id,
                                text=response_text,
                            )
                            # Send media assets resolved during post-processing
                            asset_media = result.get("asset_media", [])
                            for item in asset_media:
                                try:
                                    if item.get("send_as") == "text":
                                        await self._funnel_handoff_send(
                                            mode=_cb_mode,
                                            instance=instance,
                                            to_number=message.contact_identifier,
                                            project_id=chatbot.project_id,
                                            source_type="chatbot",
                                            source_id=chatbot.id,
                                            instance_id=instance_id,
                                            user_id=contact_id,
                                            text=item["url"],
                                        )
                                    else:
                                        await self._funnel_handoff_send(
                                            mode=_cb_mode,
                                            instance=instance,
                                            to_number=message.contact_identifier,
                                            project_id=chatbot.project_id,
                                            source_type="chatbot",
                                            source_id=chatbot.id,
                                            instance_id=instance_id,
                                            user_id=contact_id,
                                            media_url=item["url"],
                                            media_type=item["type"],
                                            caption=item.get("caption"),
                                            filename=item.get("filename"),
                                        )
                                except Exception as am_err:
                                    logger.warning(f"Failed to send funnel asset media: {am_err}")

                    return result
                return {"success": False, "error": f"Chatbot {handoff_id} not found"}

            if handoff_type == "human":
                escalation_origin = {
                    "type": "funnel",
                    "ref_id": enrollment.id,
                    "step_id": step_id,
                    "thread_index": routing_meta.get("thread_index"),
                    "return_action": routing_meta.get("return_action", "resume"),
                }
                return await self._route_to_human_inbox(
                    message,
                    project_id or enrollment.funnel.project_id,
                    escalation_origin=escalation_origin,
                    reason="Funnel human handoff",
                    contact_id=contact_id,
                )

            # Default: process through normal flow
            return await chat_service.process_incoming_message(
                channel=message.channel,
                provider_type=message.provider_type,
                message_data=message_data,
                instance_name=message.instance_name,
            )

        except Exception as e:
            logger.error(f"Error handling funnel_send message: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ── Multi-enrollment classification ─────────────────────────────────

    async def _classify_multi_enrollment(
        self, message: InboundMessage, funnel_states: list,
        step_type: str, engine,
    ) -> Optional[Dict[str, Any]]:
        """Classify a message against goals from multiple concurrent funnel enrollments.

        Returns:
            - A result dict if classification resolved (one match or ambiguous → inbox)
            - None if no enrollments have goals (caller falls through to single-enrollment path)
        """
        from app.models import FunnelEnrollment, FunnelStep, FunnelEnrollmentThread
        from app.services.funnel.milestone_classifier import MilestoneClassifier

        # Gather goals from all enrollments
        enrollment_goals = []  # [(enrollment, step, goals, config)]
        for state in funnel_states:
            if not state.handler_id:
                continue
            enrollment = self.db.query(FunnelEnrollment).filter(
                FunnelEnrollment.id == state.handler_id,
                FunnelEnrollment.status == "active",
            ).first()
            if not enrollment:
                continue

            step = enrollment.current_step
            thread = None
            if not step or step.step_type != step_type:
                thread = (
                    self.db.query(FunnelEnrollmentThread)
                    .join(FunnelStep, FunnelEnrollmentThread.current_step_id == FunnelStep.id)
                    .filter(
                        FunnelEnrollmentThread.enrollment_id == enrollment.id,
                        FunnelEnrollmentThread.status == "active",
                        FunnelStep.step_type == step_type,
                    )
                    .first()
                )
                if thread:
                    step = thread.current_step

            if not step:
                continue
            config = step.step_config or {}
            goals = config.get("goals", [])
            if goals:
                enrollment_goals.append((enrollment, step, goals, config))

        if not enrollment_goals:
            return None  # No goals on any enrollment — fall through

        if len(enrollment_goals) == 1:
            return None  # Only one enrollment has goals — fall through to single path

        # Classify against ALL goals across all enrollments
        all_goals = []
        goal_to_enrollment = {}
        for enrollment, step, goals, config in enrollment_goals:
            for goal in goals:
                # Tag each goal with its enrollment_id for disambiguation
                tagged_goal = dict(goal)
                tagged_goal["_enrollment_id"] = enrollment.id
                all_goals.append(tagged_goal)
                goal_id = goal.get("id", "")
                goal_to_enrollment[f"{enrollment.id}:{goal_id}"] = (enrollment, step, config)

        pid = enrollment_goals[0][0].funnel.project_id
        classifier = MilestoneClassifier(self.db)
        classification = classifier.classify(
            message_text=message.resolved_text or "",
            goals=all_goals,
            project_id=pid,
            content_pieces=[p.model_dump() for p in message.content_pieces],
            conversation_history=self._get_conversation_history(
                enrollment_goals[0][0],
                max_messages=10,
                project_id=pid,
            ),
        )

        if classification.outcome == "goal_met" and classification.goal_id:
            # Find which enrollment(s) this goal belongs to
            matching_enrollments = []
            for enrollment, step, goals, config in enrollment_goals:
                for goal in goals:
                    if goal.get("id") == classification.goal_id:
                        matching_enrollments.append((enrollment, step, config))

            if len(matching_enrollments) == 1:
                # Exactly one match — route to that enrollment
                enrollment, step, config = matching_enrollments[0]
                result = engine.handle_classified_reply(
                    enrollment_id=enrollment.id,
                    message=message,
                    classification=classification,
                )
                if result.get("delegate"):
                    delegate_result = await self._delegate_to_handler(
                        message, result["delegate"], enrollment, config,
                    )
                    return {"success": True, "funnel_result": result, "delegate_result": delegate_result}
                return {"success": True, "funnel_result": result}

            if len(matching_enrollments) > 1:
                # Ambiguous — multiple enrollments have matching goals
                logger.info(
                    f"Ambiguous multi-enrollment match for {message.contact_identifier}: "
                    f"{len(matching_enrollments)} enrollments match goal '{classification.goal_id}'. "
                    f"Routing to inbox."
                )
                return {"success": True, "funnel_result": {"advanced": False, "reason": "ambiguous_multi_enrollment"}}

        # No goal matched — classify against primary enrollment (first in list)
        enrollment, step, goals, config = enrollment_goals[0]
        result = engine.handle_classified_reply(
            enrollment_id=enrollment.id,
            message=message,
            classification=classification,
        )
        if result.get("delegate"):
            delegate_result = await self._delegate_to_handler(
                message, result["delegate"], enrollment, config,
            )
            return {"success": True, "funnel_result": result, "delegate_result": delegate_result}
        return {"success": True, "funnel_result": result}

    # ── Milestone classification helpers ──────────────────────────────────

    def _check_debounce(
        self, enrollment, config: dict, message: InboundMessage,
    ) -> Optional[dict]:
        """If within debounce window, store message and return debounced result."""
        from sqlalchemy.orm.attributes import flag_modified

        debounce_seconds = config.get("debounce_seconds", 3)
        if debounce_seconds <= 0:
            return None

        meta = dict(enrollment.enrollment_metadata or {})
        last_ts = meta.get("_last_classified_at")
        now = datetime.utcnow()

        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                if (now - last_dt).total_seconds() < debounce_seconds:
                    # Within debounce window — store pending message
                    pending = meta.get("_pending_messages", [])
                    pending.append({
                        "text": (message.resolved_text or "")[:500],
                        "at": now.isoformat(),
                    })
                    meta["_pending_messages"] = pending
                    enrollment.enrollment_metadata = meta
                    flag_modified(enrollment, "enrollment_metadata")
                    self.db.commit()
                    return {"debounced": True, "pending_count": len(pending)}
            except (ValueError, TypeError):
                pass

        # Check for pending messages to concatenate
        pending = meta.pop("_pending_messages", [])
        if pending:
            # Concatenate pending messages with current one
            combined_text = " ".join(p["text"] for p in pending)
            current_text = message.resolved_text or ""
            message.resolved_text = (combined_text + " " + current_text).strip()
            enrollment.enrollment_metadata = meta
            flag_modified(enrollment, "enrollment_metadata")
            self.db.commit()

        return None

    def _mark_classified(self, enrollment) -> None:
        """Record classification timestamp for debounce."""
        from sqlalchemy.orm.attributes import flag_modified

        meta = dict(enrollment.enrollment_metadata or {})
        meta["_last_classified_at"] = datetime.utcnow().isoformat()
        meta.pop("_pending_messages", None)
        enrollment.enrollment_metadata = meta
        flag_modified(enrollment, "enrollment_metadata")
        self.db.commit()

    def _get_conversation_history(
        self, enrollment, max_messages: int = 10,
        project_id: Optional[int] = None,
    ) -> list:
        """Fetch recent chat messages for AI classification context.

        Filters by project_id to ensure multi-tenant isolation.
        """
        try:
            from app.models import ChatMessage, ChatSession
            from app.models.messaging import MessagingUser

            user = self.db.query(MessagingUser).filter(
                MessagingUser.id == enrollment.user_id,
            ).first()
            if not user:
                return []

            pid = project_id or (enrollment.funnel.project_id if enrollment.funnel else None)

            identifier = user.external_id or user.phone or user.email or ""
            query = (
                self.db.query(ChatSession)
                .filter(
                    ChatSession.user_identifier == identifier,
                    ChatSession.is_active == True,
                )
            )
            # Filter by project_id via the chatbot relationship
            if pid:
                query = query.join(Chatbot, ChatSession.chatbot_id == Chatbot.id).filter(
                    Chatbot.project_id == pid,
                )
            session = query.order_by(ChatSession.created_at.desc()).first()
            if not session:
                return []

            messages = (
                self.db.query(ChatMessage)
                .filter(ChatMessage.session_id == session.id)
                .order_by(ChatMessage.created_at.desc())
                .limit(max_messages)
                .all()
            )
            return [
                {"role": m.role, "text": m.content[:200]}
                for m in reversed(messages)
            ]
        except Exception as e:
            logger.warning(f"Error fetching conversation history: {e}")
            return []

    async def _delegate_to_handler(
        self, message: InboundMessage, handler_config: dict,
        enrollment, step_config: dict,
    ) -> Dict[str, Any]:
        """Delegate a single message to an on_topic handler (chatbot/agent_team).

        Does NOT change routing state — funnel keeps ownership.
        Injects goal context into the message so the handler can steer toward the goal.
        """
        handler_type = handler_config.get("type", "chatbot")
        handler_id = handler_config.get("id")
        if not handler_id:
            return {"success": False, "error": "No handler_id in on_topic_handler config"}

        from app.services.unified_chat_service import UnifiedChatService
        chat_service = UnifiedChatService(self.db)

        # Build message_data with goal context injection
        goals = step_config.get("goals", [])
        goal_descriptions = [g.get("description", "") for g in goals if g.get("description")]
        goal_context = ""
        if goal_descriptions:
            goal_context = (
                "The user is in a funnel step. Goals: " + "; ".join(goal_descriptions)
                + ". Help guide them toward completing a goal."
            )

        message_data = {
            "message_id": message.message_id,
            "from": message.contact_identifier,
            "customer_phone": message.customer_phone,
            "body": message.resolved_text or "",
            "message_type": message.content_pieces[0].type if message.content_pieces else "text",
            "push_name": message.push_name,
            "remote_jid": message.remote_jid,
            "remote_jid_alt": message.remote_jid_alt,
            "key": (message.raw_payload or {}).get("key"),
            "raw": message.raw_payload,
            "content_pieces": [p.model_dump() for p in message.content_pieces],
            "_funnel_goal_context": goal_context,
        }

        try:
            if handler_type == "agent_team":
                team = self.db.query(AgentTeam).filter(AgentTeam.id == handler_id).first()
                if team:
                    return await chat_service._process_agent_team_message(
                        team=team,
                        provider=None,
                        customer_identifier=message.contact_identifier,
                        channel=message.channel,
                        message_data=message_data,
                    )
                return {"success": False, "error": f"Agent team {handler_id} not found"}

            if handler_type == "chatbot":
                chatbot = self.db.query(Chatbot).filter(Chatbot.id == handler_id).first()
                if chatbot:
                    return await chat_service.process_incoming_message(
                        channel=message.channel,
                        provider_type=message.provider_type,
                        message_data=message_data,
                        instance_name=message.instance_name,
                        chatbot_override=chatbot,
                    )
                return {"success": False, "error": f"Chatbot {handler_id} not found"}

            return {"success": False, "error": f"Unknown on_topic handler type: {handler_type}"}
        except Exception as e:
            logger.error(f"Error delegating to on_topic handler: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ── Opt-out & dedup helpers ──────────────────────────────────────────

    _STOP_KEYWORDS = {"stop", "unsubscribe", "optout", "opt-out", "cancel", "parar", "sair", "cancelar"}

    def _check_opt_out(
        self, project_id: int, message: InboundMessage, resolved_contact=None,
    ) -> Optional[Dict[str, Any]]:
        """Check if contact has opted out of this channel (G1, G11).

        Also detects STOP keywords and auto-opts-out the contact.
        Returns a result dict if opted out, None otherwise.
        """
        from sqlalchemy.orm.attributes import flag_modified

        body = (message.resolved_text or "").strip().lower()
        user = resolved_contact

        # STOP keyword detection — auto-add channel to opted_out_channels
        if body in self._STOP_KEYWORDS and user:
            channels = list(user.opted_out_channels or [])
            if message.channel not in channels:
                channels.append(message.channel)
                user.opted_out_channels = channels
                flag_modified(user, "opted_out_channels")
                self.db.commit()
                logger.info(
                    f"Contact {message.contact_identifier} opted out of {message.channel} "
                    f"(STOP keyword)"
                )
            return {
                "success": True,
                "opted_out": True,
                "reason": "stop_keyword",
                "channel": message.channel,
            }

        if not user:
            return None

        # Global opt-out
        if user.global_opt_out:
            logger.info(f"Contact {message.contact_identifier} has global opt-out")
            return {
                "success": True,
                "opted_out": True,
                "reason": "global_opt_out",
            }

        # Per-channel opt-out
        opted_channels = user.opted_out_channels or []
        if message.channel in opted_channels:
            logger.info(f"Contact {message.contact_identifier} opted out of {message.channel}")
            return {
                "success": True,
                "opted_out": True,
                "reason": "channel_opt_out",
                "channel": message.channel,
            }

        return None

    def _check_blocked(
        self, project_id: int, message: InboundMessage, resolved_contact=None,
    ) -> Optional[Dict[str, Any]]:
        """Check if contact is blocked — silently drop all messages from blocked contacts."""
        user = resolved_contact

        if user and user.is_blocked:
            logger.info(
                f"Blocked contact {message.contact_identifier} on project {project_id} "
                f"— silently dropping {message.channel} message"
            )
            return {
                "success": True,
                "blocked": True,
            }

        return None

    def _check_inbound_dedup(self, project_id: int, message_id: str) -> bool:
        """Check if the same message_id was already processed within 5 seconds (G15)."""
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(seconds=5)
        duplicate = self.db.query(MessagingEvent).filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.event_name == "message_received",
            MessagingEvent.created_at > cutoff,
            MessagingEvent.properties["message_id"].as_string() == message_id,
        ).first()

        return duplicate is not None

    def _stamp_reply_on_send_log(
        self,
        project_id: int,
        contact: MessagingUser,
        message: InboundMessage,
    ) -> None:
        """Attribute an inbound message as a reply to the most recent outbound send.

        Finds the newest non-failed SendLog for this contact on the same
        channel whose MES attribution window is still open, stamps
        replied_at/reply_count, emits channel.<ch>.replied, and marks the MES
        score stale so reach recomputes with the reply signal.
        """
        from app.models import SendLog, MESConfig

        channel = (message.channel or "whatsapp").lower()
        now = datetime.utcnow()

        config = self.db.query(MESConfig).filter(
            MESConfig.project_id == project_id,
        ).first()
        window_h = config.attribution_window_hours if config else 24
        cutoff = now - timedelta(hours=window_h)

        send_log = (
            self.db.query(SendLog)
            .filter(
                SendLog.project_id == project_id,
                SendLog.user_id == contact.id,
                SendLog.sent_at.isnot(None),
                SendLog.sent_at >= cutoff,
                SendLog.status.notin_(["failed", "skipped", "exhausted", "blocked", "superseded"]),
                (SendLog.channel == channel) | (SendLog.resolved_channel == channel),
            )
            .order_by(SendLog.sent_at.desc())
            .first()
        )
        if not send_log:
            return

        first_reply = send_log.replied_at is None
        send_log.reply_count = (send_log.reply_count or 0) + 1
        if first_reply:
            send_log.replied_at = now
            reply_delay_s = (now - send_log.sent_at).total_seconds()
            event = MessagingEvent(
                project_id=project_id,
                user_id=contact.id,
                event_name=f"channel.{channel}.replied",
                source="delivery_tracker",
                properties={
                    "send_log_id": send_log.id,
                    "channel": channel,
                    "template_id": send_log.template_id,
                    "source_type": send_log.source_type,
                    "source_id": send_log.source_id,
                    "reply_delay_seconds": round(reply_delay_s, 1),
                    "inbound_message_id": message.message_id,
                },
                processed=False,
            )
            self.db.add(event)

        try:
            from app.services.scoring.mes_engine import MESEngine
            MESEngine(self.db).mark_stale(send_log.id)
        except Exception:
            pass

        logger.info(
            "Reply attributed to send_log %d (channel=%s, count=%d)",
            send_log.id, channel, send_log.reply_count,
        )

    def _emit_event(
        self,
        project_id: Optional[int],
        event_name: str,
        properties: Optional[dict] = None,
        user_id: Optional[int] = None,
    ) -> None:
        """Create a MessagingEvent for the event pipeline."""
        if not project_id:
            return
        try:
            event = MessagingEvent(
                project_id=project_id,
                user_id=user_id,
                event_name=event_name,
                properties=properties or {},
                source="system",
                processed=False,
            )
            self.db.add(event)
            self.db.commit()
        except Exception as e:
            logger.warning(f"Failed to emit {event_name} event: {e}")
