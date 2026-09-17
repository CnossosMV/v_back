"""
Unified Chat Service

Handles message routing across multiple channels (Web, WhatsApp via Evolution API,
Twilio SMS/WhatsApp) with support for human intervention and agent routing.
"""

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models import (
    ChatSession, ChatMessage, Chatbot, MessagingProvider,
    SupportTicket, ChatbotAgentRouting, AgentConfig, WhatsAppInstance,
    AgentTeam, ContactRoutingState,
)
from app.services.twilio_service import twilio_service
from app.services.evolution_api_service import evolution_api_service
from app.services.whatsapp_sender import infer_media_type
from app.services.chatbot.langchain_service import LangChainService
from app.services.chatbot.conversation_manager import ConversationManager
from app.services.chatbot.orchestration_engine import OrchestrationEngine
from cryptography.fernet import Fernet
import os
import base64

logger = logging.getLogger(__name__)


class UnifiedChatService:
    """Service for unified message processing across all channels."""

    def __init__(self, db: Session):
        """
        Initialize the unified chat service.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db
        self.conversation_manager = ConversationManager(db)

    def _get_instance_token(self, instance: WhatsAppInstance) -> Optional[str]:
        """Get decrypted instance token from WhatsApp instance."""
        try:
            if not instance.instance_key:
                return None

            # Try to decrypt the token
            key = os.getenv("ENCRYPTION_KEY")
            if not key:
                # Fallback: if token looks unencrypted, use directly
                if len(instance.instance_key) > 32:
                    return instance.instance_key
                return None

            f = Fernet(key.encode())
            return f.decrypt(instance.instance_key.encode()).decode()
        except Exception as e:
            logger.error(f"Error decrypting instance token: {e}")
            # Fallback: try using the key directly if decryption fails
            if instance.instance_key and len(instance.instance_key) > 32:
                return instance.instance_key
            return None

    async def process_incoming_message(
        self,
        channel: str,
        provider_type: str,
        message_data: Dict[str, Any],
        provider_id: Optional[int] = None,
        instance_name: Optional[str] = None,
        chatbot_override: Optional["Chatbot"] = None,
        skip_send: bool = False,
    ) -> Dict[str, Any]:
        """
        Process an incoming message from any channel.

        Args:
            channel: Message channel (web, whatsapp, sms)
            provider_type: Provider type (evolution_api, twilio_sms, twilio_whatsapp)
            message_data: Parsed message data
            provider_id: Optional provider ID
            instance_name: Optional Evolution API instance name
            chatbot_override: Optional explicit Chatbot to use (e.g. from funnel handoff),
                bypasses the normal instance-based responder lookup.

        Returns:
            Dict with processing result
        """
        try:
            # Extract customer identifier
            customer_identifier = self._extract_customer_identifier(message_data, channel)
            if not customer_identifier:
                logger.error("Could not extract customer identifier from message")
                return {"success": False, "error": "Invalid customer identifier"}

            # Use explicit chatbot if provided (e.g. funnel handoff), otherwise discover
            if chatbot_override:
                responder = chatbot_override
                responder_type = "chatbot"
                provider = None
            else:
                # Find the chatbot or agent team and provider
                responder, responder_type, provider = await self._find_responder(
                    provider_id=provider_id,
                    provider_type=provider_type,
                    instance_name=instance_name
                )

            if not responder:
                logger.warning(f"No chatbot or agent team found for provider {provider_id or instance_name}")
                return {"success": False, "error": "No chatbot configured"}

            # === Agent Team path ===
            if responder_type == "agent_team":
                return await self._process_agent_team_message(
                    team=responder,
                    provider=provider,
                    customer_identifier=customer_identifier,
                    channel=channel,
                    message_data=message_data,
                )

            # === Chatbot path (original flow) ===
            chatbot = responder

            # Get or create session
            session = self._get_or_create_session(
                chatbot_id=chatbot.id,
                customer_identifier=customer_identifier,
                channel=channel,
                provider=provider,
                external_conversation_id=message_data.get("conversation_id")
            )

            # Store the incoming message
            customer_message = self._store_message(
                session_id=session.id,
                content=message_data.get("body") or message_data.get("content", ""),
                role="user",
                sender_type="customer",
                external_message_id=message_data.get("message_sid") or message_data.get("message_id"),
                content_pieces=message_data.get("content_pieces"),
            )

            # Check if session is under human takeover
            if session.human_takeover:
                logger.info(f"Session {session.id} is under human takeover, skipping bot response")
                # Notify support inbox about new message
                await self._notify_support_inbox(session, customer_message)
                return {
                    "success": True,
                    "session_id": session.id,
                    "human_takeover": True,
                    "message_id": customer_message.id
                }

            # Automation pause: operator handles this contact manually — the
            # inbound message is stored and surfaced in the inbox (above),
            # but no bot auto-reply. Unlike block, inbound is never dropped.
            if self._is_contact_paused(chatbot.project_id, customer_identifier):
                logger.info(
                    f"Contact {customer_identifier} automations paused, skipping bot response"
                )
                await self._notify_support_inbox(session, customer_message)
                return {
                    "success": True,
                    "session_id": session.id,
                    "automations_paused": True,
                    "message_id": customer_message.id,
                }

            # Check if this is a manual chatbot (no auto-respond)
            # Manual chatbots should route directly to human support
            # Skip this check when chatbot_override is set (funnel handoff explicitly chose this bot)
            if not chatbot.auto_respond_whatsapp and not chatbot_override:
                logger.info(f"Manual chatbot {chatbot.id}, transferring to human support")
                # Get the customer phone from the remoteJid (extracted in webhook)
                customer_phone = message_data.get("customer_phone")
                remote_jid = message_data.get("remote_jid")
                # Evolution API v2.3.7+ provides remoteJidAlt with real phone for LID contacts
                remote_jid_alt = message_data.get("remote_jid_alt")
                # Get the message key for quoted replies (needed for LID contacts)
                message_key = message_data.get("key")
                ticket = await self.transfer_to_human(
                    session_id=session.id,
                    reason="Manual chatbot - requires human agent",
                    customer_phone=customer_phone,
                    customer_name=message_data.get("push_name"),
                    remote_jid=remote_jid,
                    remote_jid_alt=remote_jid_alt,
                    last_message_key=message_key,
                    escalation_origin=message_data.get("_escalation_origin"),
                )
                return {
                    "success": True,
                    "session_id": session.id,
                    "human_takeover": True,
                    "ticket_id": ticket.id if ticket else None,
                    "message_id": customer_message.id,
                    "manual_chatbot": True
                }

            # Check routing configuration
            routing_result = await self._check_routing(
                chatbot=chatbot,
                session=session,
                message_content=customer_message.content
            )

            if routing_result["escalate_to_human"]:
                # Create support ticket and transfer to human
                ticket = await self.transfer_to_human(
                    session_id=session.id,
                    reason=routing_result.get("reason", "Automatic escalation"),
                    escalation_origin=message_data.get("_escalation_origin"),
                )
                return {
                    "success": True,
                    "session_id": session.id,
                    "human_takeover": True,
                    "ticket_id": ticket.id if ticket else None,
                    "message_id": customer_message.id
                }

            # Generate bot response
            response = await self._generate_bot_response(
                chatbot=chatbot,
                session=session,
                message_content=customer_message.content
            )

            # Post-process [ASSET:slug] tokens in response
            asset_media = []
            try:
                from app.services.media_asset_service import MediaAssetService
                asset_svc = MediaAssetService(self.db)
                cleaned_text, resolved_items = asset_svc.extract_asset_references(
                    response["message"], chatbot.project_id
                )
                response["message"] = cleaned_text
                asset_media = resolved_items
            except Exception as e:
                logger.warning(f"Asset post-processing error: {e}")

            # Store bot response
            bot_message = self._store_message(
                session_id=session.id,
                content=response["message"],
                role="assistant",
                sender_type="bot",
                retrieved_documents=response.get("retrieved_documents"),
                token_usage=response.get("token_usage")
            )

            # Send response back to customer (skip when caller handles sending)
            if not skip_send:
                await self._send_outgoing_message(
                    session=session,
                    provider=provider,
                    content=response["message"]
                )

            # Send media assets via WhatsApp after text message (skip when caller handles sending)
            if not skip_send and asset_media and session.channel == "whatsapp" and chatbot.whatsapp_instance:
                for item in asset_media:
                    try:
                        if item.get("send_as") == "text":
                            await self._send_outgoing_message(
                                session=session,
                                provider=provider,
                                content=item["url"],
                            )
                        else:
                            await self._send_outgoing_message(
                                session=session,
                                provider=provider,
                                content=item.get("caption") or "",
                                media_url=item["url"],
                                media_type=item["type"],
                                filename=item.get("filename"),
                            )
                    except Exception as e:
                        logger.warning(f"Failed to send chatbot asset media via SendService: {e}")

            return {
                "success": True,
                "session_id": session.id,
                "message_id": customer_message.id,
                "response_id": bot_message.id,
                "response": response["message"],
                "asset_media": asset_media,
            }

        except Exception as e:
            logger.error(f"Error processing incoming message: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def send_outgoing_message(
        self,
        session_id: int,
        content: str,
        sender_type: str = "bot",
        sent_by_user_id: Optional[int] = None,
        existing_message: Optional[ChatMessage] = None,
        media_url: Optional[str] = None,
        media_type: Optional[str] = None,
        filename: Optional[str] = None,
        channel_override: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Send an outgoing message to a session.

        Args:
            session_id: Chat session ID
            content: Message content
            sender_type: Type of sender (bot, human_agent)
            sent_by_user_id: User ID if sent by human agent
            existing_message: If provided, skip storing a new message and
                              use this one for delivery-status updates.
            media_url: URL of media attachment (image, video, audio, document)
            media_type: Type of media (image, video, audio, document)
            filename: Original filename for document attachments

        Returns:
            Dict with send result
        """
        session = self.db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if not session:
            return {"success": False, "error": "Session not found"}

        # Store message (or reuse existing)
        if existing_message is not None:
            message = existing_message
        else:
            role = "assistant" if sender_type == "bot" else "assistant"
            message = self._store_message(
                session_id=session_id,
                content=content,
                role=role,
                sender_type=sender_type,
                sent_by_user_id=sent_by_user_id
            )

        # Get provider and send
        provider = session.provider
        result = await self._send_outgoing_message(
            session=session,
            provider=provider,
            content=content,
            media_url=media_url,
            media_type=media_type,
            filename=filename,
            channel_override=channel_override,
            sender_type=sender_type,
            attachments=attachments,
        )

        # Update delivery status
        ext_id = result.get("message_id") or result.get("message_sid")
        if result.get("success"):
            message.delivery_status = "sent"
            message.external_message_id = ext_id
        else:
            message.delivery_status = "failed"

        self.db.commit()

        return {
            "success": result.get("success", False),
            "message_id": message.id,
            "external_message_id": ext_id,
        }

    async def transfer_to_human(
        self,
        session_id: int,
        user_id: Optional[int] = None,
        reason: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_name: Optional[str] = None,
        remote_jid: Optional[str] = None,
        remote_jid_alt: Optional[str] = None,
        last_message_key: Optional[Dict] = None,
        escalation_origin: Optional[Dict] = None
    ) -> Optional[SupportTicket]:
        """
        Transfer a conversation to human support.

        Args:
            session_id: Chat session ID
            user_id: Optional user ID to assign the ticket to
            reason: Reason for escalation
            customer_phone: Customer's phone number (for WhatsApp when identifier is LID)
            customer_name: Customer's display name
            remote_jid: WhatsApp remoteJid for direct replies (may be LID format)
            remote_jid_alt: Alternative JID with real phone number (v2.3.7+, for LID contacts)
            last_message_key: Last message key for quoted replies (needed for LID contacts)

        Returns:
            Created SupportTicket or None
        """
        session = self.db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if not session:
            logger.error(f"Session {session_id} not found for transfer")
            return None

        # Check if ticket already exists
        existing_ticket = self.db.query(SupportTicket).filter(
            SupportTicket.session_id == session_id,
            SupportTicket.status.in_(["open", "in_progress", "waiting_customer"])
        ).first()

        if existing_ticket:
            logger.info(f"Ticket already exists for session {session_id}")
            # Update the takeover status and any missing customer info
            session.human_takeover = True
            session.human_takeover_at = datetime.utcnow()
            # Update customer phone if we now have it
            if customer_phone and not existing_ticket.customer_phone:
                existing_ticket.customer_phone = customer_phone
            # Update customer name if we now have it
            if customer_name and not existing_ticket.customer_name:
                existing_ticket.customer_name = customer_name
            # Backfill contact_id from session if ticket lacks it
            if session.contact_id and not existing_ticket.contact_id:
                existing_ticket.contact_id = session.contact_id
            # Update metadata with remote_jid, remote_jid_alt and last_message_key
            metadata = existing_ticket.ticket_metadata or {}
            if remote_jid:
                metadata["remote_jid"] = remote_jid
            if remote_jid_alt:
                metadata["remote_jid_alt"] = remote_jid_alt
            if last_message_key:
                metadata["last_message_key"] = last_message_key
            existing_ticket.ticket_metadata = metadata
            self.db.commit()
            return existing_ticket

        # Generate ticket number
        ticket_number = self._generate_ticket_number(session.chatbot.project_id)

        # Build ticket metadata
        ticket_metadata = {}
        if remote_jid:
            ticket_metadata["remote_jid"] = remote_jid
        if remote_jid_alt:
            ticket_metadata["remote_jid_alt"] = remote_jid_alt
        if last_message_key:
            ticket_metadata["last_message_key"] = last_message_key

        # Enrich customer details from MessagingUser if not provided by caller
        resolved_contact_id = session.contact_id
        if resolved_contact_id and (not customer_name or not customer_phone):
            from app.models import MessagingUser
            contact = self.db.query(MessagingUser).filter(
                MessagingUser.id == resolved_contact_id
            ).first()
            if contact:
                if not customer_name and contact.name:
                    customer_name = contact.name
                if not customer_phone and contact.phone:
                    customer_phone = contact.phone

        # Create support ticket
        ticket = SupportTicket(
            project_id=session.chatbot.project_id,
            chatbot_id=session.chatbot_id,
            session_id=session_id,
            ticket_number=ticket_number,
            status="open",
            priority="medium",
            assigned_to_user_id=user_id,
            human_takeover=True,
            bot_can_resume=True,
            customer_identifier=session.user_identifier,
            customer_phone=customer_phone,
            customer_name=customer_name,
            channel=session.channel,
            escalation_reason=reason,
            escalation_origin=escalation_origin,
            ticket_metadata=ticket_metadata,
            contact_id=resolved_contact_id,
        )

        self.db.add(ticket)

        # Update session
        session.human_takeover = True
        session.human_takeover_at = datetime.utcnow()
        session.support_ticket_id = ticket.id

        self.db.commit()
        self.db.refresh(ticket)

        # Insert system message as ticket boundary marker
        sys_msg = ChatMessage(
            session_id=session_id,
            role="system",
            sender_type="system",
            content=f"Ticket #{ticket_number} opened",
            channel=session.channel,
            message_metadata={
                "event_type": "ticket_opened",
                "ticket_id": ticket.id,
                "ticket_number": ticket_number,
            },
        )
        self.db.add(sys_msg)
        self.db.commit()

        logger.info(f"Created support ticket {ticket_number} for session {session_id}")

        # Push notification to agents
        try:
            from app.services.push_notification_service import get_push_service
            push_svc = get_push_service(self.db)
            project_id = session.chatbot.project_id
            if ticket.assigned_to_user_id:
                push_svc.send_notification(
                    ticket.assigned_to_user_id,
                    "New Support Ticket",
                    f"#{ticket_number} — {ticket.customer_name or ticket.customer_identifier}",
                    {"url": f"/support/{ticket.id}", "ticket_id": str(ticket.id), "project_id": str(project_id)},
                )
            else:
                push_svc.send_to_project_agents(
                    project_id,
                    "New Support Ticket",
                    f"#{ticket_number} — {ticket.customer_name or ticket.customer_identifier}",
                    {"url": f"/support/{ticket.id}", "ticket_id": str(ticket.id), "project_id": str(project_id)},
                )
        except Exception as e:
            logger.warning(f"Push notification failed for ticket {ticket_number}: {e}")

        return ticket

    async def resume_bot(self, session_id: int) -> bool:
        """
        Resume bot handling for a conversation.

        Args:
            session_id: Chat session ID

        Returns:
            True if successful
        """
        session = self.db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if not session:
            return False

        # Check if ticket allows bot to resume
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.session_id == session_id
        ).order_by(SupportTicket.created_at.desc()).first()

        if ticket and not ticket.bot_can_resume:
            logger.warning(f"Bot cannot resume for session {session_id}")
            return False

        # Update session
        session.human_takeover = False
        session.bot_resumed_at = datetime.utcnow()

        # Update ticket status
        if ticket:
            ticket.status = "resolved"
            ticket.human_takeover = False
            ticket.resolved_at = datetime.utcnow()

        self.db.commit()

        logger.info(f"Bot resumed for session {session_id}")
        return True

    def _extract_customer_identifier(
        self,
        message_data: Dict[str, Any],
        channel: str
    ) -> Optional[str]:
        """Extract customer identifier from message data."""
        # Try various field names
        identifier = (
            message_data.get("from_number") or
            message_data.get("from") or
            message_data.get("sender") or
            message_data.get("customer_identifier") or
            message_data.get("user_identifier")
        )

        # For Evolution API
        if not identifier and "key" in message_data:
            key = message_data.get("key", {})
            identifier = key.get("remoteJid", "").replace("@s.whatsapp.net", "")

        return identifier

    def _is_contact_paused(self, project_id: int, customer_identifier: str) -> bool:
        """True if the contact has automations paused (identifier mapping
        mirrors InboundRouter._check_blocked: project + external_id)."""
        from app.models.messaging import MessagingUser

        try:
            user = self.db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.external_id == customer_identifier,
            ).first()
            return bool(user and user.automations_paused)
        except Exception as e:
            logger.warning(f"Automation pause lookup failed for {customer_identifier}: {e}")
            return False

    async def _find_responder(
        self,
        provider_id: Optional[int] = None,
        provider_type: Optional[str] = None,
        instance_name: Optional[str] = None
    ) -> tuple:
        """
        Find chatbot or agent team and provider from available identifiers.

        Returns:
            (responder, responder_type, provider) where:
            - responder: Chatbot or AgentTeam object
            - responder_type: "chatbot" or "agent_team"
            - provider: MessagingProvider or None
        """
        provider = None
        responder = None
        responder_type = None

        if provider_id:
            provider = self.db.query(MessagingProvider).filter(
                MessagingProvider.id == provider_id,
                MessagingProvider.is_active == True
            ).first()
            if provider:
                responder = provider.chatbot
                responder_type = "chatbot"

        elif instance_name:
            # Evolution API - find by instance name
            instance = self.db.query(WhatsAppInstance).filter(
                WhatsAppInstance.instance_name == instance_name
            ).first()
            if instance:
                # Use handler_channel_links for lookup
                from app.services.handler_channel_link_service import HandlerChannelLinkService
                link_svc = HandlerChannelLinkService(self.db)
                handlers = link_svc.find_handlers_by_instance("whatsapp", instance.id)

                # Priority 1: chatbot from link table
                for h in handlers:
                    if h["handler_type"] == "chatbot":
                        chatbot = self.db.query(Chatbot).filter(
                            Chatbot.id == h["handler_id"], Chatbot.status == "active"
                        ).first()
                        if chatbot:
                            logger.info(f"Found chatbot {chatbot.id} ({chatbot.name}) for instance {instance_name} via link table")
                            responder = chatbot
                            responder_type = "chatbot"
                            break

                # Priority 2: agent team from link table
                if not responder:
                    for h in handlers:
                        if h["handler_type"] == "agent_team":
                            team = self.db.query(AgentTeam).filter(
                                AgentTeam.id == h["handler_id"], AgentTeam.status == "active"
                            ).first()
                            if team:
                                logger.info(f"Found agent team {team.id} ({team.name}) for instance {instance_name} via link table")
                                responder = team
                                responder_type = "agent_team"
                                break

                # Legacy fallback: direct FK
                if not responder:
                    chatbot = self.db.query(Chatbot).filter(
                        Chatbot.whatsapp_instance_id == instance.id,
                        Chatbot.status == "active"
                    ).first()
                    if chatbot:
                        logger.info(f"Found chatbot {chatbot.id} ({chatbot.name}) for instance {instance_name} via legacy FK")
                        responder = chatbot
                        responder_type = "chatbot"
                    else:
                        team = self.db.query(AgentTeam).filter(
                            AgentTeam.whatsapp_instance_id == instance.id,
                            AgentTeam.status == "active"
                        ).first()
                        if team:
                            logger.info(f"Found agent team {team.id} ({team.name}) for instance {instance_name} via legacy FK")
                            responder = team
                            responder_type = "agent_team"

        return responder, responder_type, provider

    async def _find_chatbot_and_provider(
        self,
        provider_id: Optional[int] = None,
        provider_type: Optional[str] = None,
        instance_name: Optional[str] = None
    ) -> tuple:
        """Backwards-compatible wrapper around _find_responder."""
        responder, responder_type, provider = await self._find_responder(
            provider_id=provider_id,
            provider_type=provider_type,
            instance_name=instance_name,
        )
        if responder_type == "chatbot":
            return responder, provider
        return None, provider

    async def _process_agent_team_message(
        self,
        team: AgentTeam,
        provider: Optional[MessagingProvider],
        customer_identifier: str,
        channel: str,
        message_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Process an incoming message through the Agent Team orchestration engine.

        Routes to OrchestrationEngine instead of the old chatbot flow.
        """
        message_content = message_data.get("body") or message_data.get("content", "")

        # Check if manual team (no auto-respond)
        if not team.auto_respond_whatsapp:
            logger.info(f"Manual agent team {team.id}, skipping auto-response")
            return {"success": True, "manual_team": True}

        # Automation pause: no agent-team auto-reply while an operator
        # handles the contact manually (inbound stays recorded upstream).
        if self._is_contact_paused(team.project_id, customer_identifier):
            logger.info(
                f"Contact {customer_identifier} automations paused, skipping agent team response"
            )
            return {"success": True, "automations_paused": True}

        try:
            engine = OrchestrationEngine(self.db)
            result = engine.process_message(
                team_id=team.id,
                message=message_content,
                user_identifier=customer_identifier,
                channel=channel,
                content_pieces=message_data.get("content_pieces"),
            )

            response_text = result.get("response", "")
            session_id = result.get("session_id")
            images = result.get("images", [])

            # Post-process [ASSET:slug] tokens in response
            asset_media = []
            if response_text:
                try:
                    from app.services.media_asset_service import MediaAssetService
                    asset_svc = MediaAssetService(self.db)
                    response_text, resolved_items = asset_svc.extract_asset_references(
                        response_text, team.project_id
                    )
                    asset_media = resolved_items
                except Exception as e:
                    logger.warning(f"Agent team asset post-processing error: {e}")

            # Send response back via the channel
            if response_text and session_id:
                session = self.db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if session:
                    await self._send_agent_team_response(
                        team=team,
                        session=session,
                        provider=provider,
                        content=response_text,
                        images=images,
                        asset_media=asset_media,
                    )

            return {
                "success": True,
                "session_id": session_id,
                "response": response_text,
                "responder_type": "agent_team",
            }

        except Exception as e:
            logger.error(f"Error processing agent team message: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _resolve_image_url(path: str) -> str:
        """Resolve an image path to a publicly reachable URL."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        public_base = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_BASE_URL", "")
        if path.startswith("/"):
            return f"{public_base}{path}"
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"{public_base}/api/v1/knowledge-base/images/{filename}"

    async def _send_agent_team_response(
        self,
        team: AgentTeam,
        session: ChatSession,
        provider: Optional[MessagingProvider],
        content: str,
        images: Optional[List[str]] = None,
        asset_media: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Send agent team response back through the appropriate channel.

        Uses _resolve_outbound_target() to route replies through the same
        channel/instance the customer used inbound.
        """
        import os as _os

        channel = session.channel

        if channel == "web":
            return {"success": True, "channel": "web"}

        target = self._resolve_outbound_target(session, team=team)

        try:
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService

            instance_config = None
            if target["instance_id"]:
                instance_config = {"instance_id": target["instance_id"]}

            svc = SendService(self.db)
            recipient = target["recipient"]

            out_content = OutboundContent(content_type="text", text=content)
            decision = await svc.send(
                project_id=team.project_id,
                user_id=session.user_id,
                recipient=recipient,
                content=out_content,
                channel=channel,
                source_type="agent_team",
                source_id=team.id,
                instance_config=instance_config,
            )

            if decision.success:
                if images:
                    for img_path in images:
                        try:
                            img_url = self._resolve_image_url(img_path)
                            media_content = OutboundContent(
                                content_type="media",
                                media_url=img_url,
                                media_type=infer_media_type(img_url),
                            )
                            await svc.send(
                                project_id=team.project_id,
                                user_id=session.user_id,
                                recipient=recipient,
                                content=media_content,
                                channel=channel,
                                source_type="agent_team",
                                source_id=team.id,
                                instance_config=instance_config,
                            )
                        except Exception as img_err:
                            logger.warning(f"Failed to send image via send layer: {img_err}")

                if asset_media:
                    for item in asset_media:
                        try:
                            if item.get("send_as") == "text":
                                mc = OutboundContent(content_type="text", text=item["url"])
                            else:
                                mc = OutboundContent(
                                    content_type="media",
                                    media_url=item["url"],
                                    media_type=item["type"],
                                    media_caption=item.get("caption"),
                                )
                            await svc.send(
                                project_id=team.project_id,
                                user_id=session.user_id,
                                recipient=recipient,
                                content=mc,
                                channel=channel,
                                source_type="agent_team",
                                source_id=team.id,
                                instance_config=instance_config,
                            )
                        except Exception as asset_err:
                            logger.warning(f"Failed to send asset media via send layer: {asset_err}")

                from app.services.channels.consolidation import ledger_in_send_enabled
                if session.user_id and not ledger_in_send_enabled(self.db, team.project_id):
                    try:
                        from app.services.scoring.policy_service import PolicyService
                        PolicyService(self.db).record_contact(
                            project_id=team.project_id,
                            user_id=session.user_id,
                            channel=decision.channel_used or channel,
                            source="agent_team",
                            source_id=team.id,
                        )
                        self.db.commit()
                    except Exception as e:
                        logger.warning(f"Error recording contact ledger: {e}")

            return {
                "success": decision.success,
                "send_log_id": decision.send_log_id,
                "channel": decision.channel_used,
                "error": decision.error,
                "message_id": decision.provider_message_id,
            }
        except Exception as e:
            logger.error(f"SendService agent team error: {e}")
            return {"success": False, "error": str(e)}

    def _get_or_create_session(
        self,
        chatbot_id: int,
        customer_identifier: str,
        channel: str,
        provider: Optional[MessagingProvider] = None,
        external_conversation_id: Optional[str] = None
    ) -> ChatSession:
        """Get or create a chat session."""
        # Try to find existing active session
        session = self.db.query(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id,
            ChatSession.user_identifier == customer_identifier,
            ChatSession.channel == channel,
            ChatSession.is_active == True
        ).first()

        if session:
            session.last_interaction_at = datetime.utcnow()
            self.db.commit()
            return session

        # Create new session
        session = ChatSession(
            chatbot_id=chatbot_id,
            user_identifier=customer_identifier,
            channel=channel,
            provider_id=provider.id if provider else None,
            external_conversation_id=external_conversation_id,
            is_active=True
        )

        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)

        return session

    def _store_message(
        self,
        session_id: int,
        content: str,
        role: str,
        sender_type: str,
        sent_by_user_id: Optional[int] = None,
        external_message_id: Optional[str] = None,
        retrieved_documents: Optional[List[Dict]] = None,
        token_usage: Optional[Dict] = None,
        content_pieces: Optional[List[Dict]] = None,
        resolved_text: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> ChatMessage:
        """Store a chat message."""
        # Resolve channel from session if not provided
        msg_channel = channel
        if not msg_channel:
            session = self.db.query(ChatSession).filter(ChatSession.id == session_id).first()
            msg_channel = session.channel if session else None

        message = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            sender_type=sender_type,
            sent_by_user_id=sent_by_user_id,
            external_message_id=external_message_id,
            retrieved_documents=retrieved_documents or [],
            prompt_tokens=token_usage.get("prompt_tokens") if token_usage else None,
            completion_tokens=token_usage.get("completion_tokens") if token_usage else None,
            total_tokens=token_usage.get("total_tokens") if token_usage else None,
            content_pieces=content_pieces,
            resolved_text=resolved_text,
            channel=msg_channel,
        )

        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)

        return message

    async def _check_routing(
        self,
        chatbot: Chatbot,
        session: ChatSession,
        message_content: str
    ) -> Dict[str, Any]:
        """Check routing configuration and decide if should escalate."""
        # Get routing config
        routing = self.db.query(ChatbotAgentRouting).filter(
            ChatbotAgentRouting.chatbot_id == chatbot.id
        ).first()

        if not routing:
            # Default: bot handles all
            return {"escalate_to_human": False}

        # Check routing mode
        if routing.routing_mode == "human_only":
            return {
                "escalate_to_human": True,
                "reason": "Routing mode is human_only"
            }

        if routing.routing_mode == "bot_only":
            return {"escalate_to_human": False}

        # For bot_then_human or custom, check escalation keywords
        if routing.escalation_keywords:
            message_lower = message_content.lower()
            for keyword in routing.escalation_keywords:
                if keyword.lower() in message_lower:
                    return {
                        "escalate_to_human": True,
                        "reason": f"Escalation keyword detected: {keyword}"
                    }

        # Check max bot turns
        if routing.max_bot_turns:
            message_count = self.db.query(ChatMessage).filter(
                ChatMessage.session_id == session.id,
                ChatMessage.sender_type == "bot"
            ).count()

            if message_count >= routing.max_bot_turns:
                return {
                    "escalate_to_human": True,
                    "reason": f"Max bot turns ({routing.max_bot_turns}) exceeded"
                }

        return {"escalate_to_human": False}

    async def _generate_bot_response(
        self,
        chatbot: Chatbot,
        session: ChatSession,
        message_content: str
    ) -> Dict[str, Any]:
        """Generate a bot response using LangChain."""
        try:
            from app.services.chatbot.llm_key_resolver import resolve_llm, resolve_embeddings, create_embeddings
            from app.services.chatbot.vector_store import VectorStoreService
            from app.services.chatbot.knowledge_loader import KnowledgeLoader

            project_id = chatbot.project_id if chatbot.project_id else 0
            llm_cfg = resolve_llm(self.db, project_id, "chat")
            emb_cfg = resolve_embeddings(self.db, project_id)
            embeddings = create_embeddings(emb_cfg)

            vector_store_path = os.getenv("VECTOR_STORE_PATH", "/app/data/vector_stores")
            vector_store = VectorStoreService(vector_store_path, embeddings)
            knowledge_loader = KnowledgeLoader()

            langchain_service = LangChainService(
                openai_api_key=llm_cfg.api_key,
                vector_store_service=vector_store,
                knowledge_loader=knowledge_loader,
                model_name=llm_cfg.model,
                temperature=llm_cfg.temperature,
            )

            # Get conversation history
            history = self.conversation_manager.get_conversation_history(
                session_id=session.id,
                limit=10
            )

            # Build effective system prompt with media asset catalog
            effective_prompt = chatbot.system_prompt or ""
            try:
                from app.services.media_asset_service import MediaAssetService
                asset_catalog = MediaAssetService(self.db).build_asset_catalog(
                    project_id=project_id,
                    chatbot_id=chatbot.id,
                )
                if asset_catalog:
                    effective_prompt = (effective_prompt + "\n\n" + asset_catalog).strip()
                    logger.info(f"Asset catalog injected for chatbot {chatbot.id}: {len(asset_catalog)} chars")
            except Exception as e:
                logger.warning(f"Failed to build asset catalog for chatbot: {e}")

            # Inject knowledge library context if chatbot has collection bindings
            try:
                from app.services.knowledge.retrieval_service import RetrievalService
                retrieval = RetrievalService(self.db, openai_api_key)
                lib_results = retrieval.retrieve_for_rag(
                    project_id=project_id,
                    query=message_content,
                    consumer_type="chatbot",
                    consumer_id=chatbot.id,
                    k=5,
                    min_score=0.3,
                )
                if lib_results.get("results"):
                    lib_parts = []
                    for i, r in enumerate(lib_results["results"], 1):
                        lib_parts.append(f"[Library Source {i}] ({r['asset_name']})\n{r['chunk_text']}")
                    lib_ctx = "\n---\n".join(lib_parts)
                    effective_prompt = (
                        effective_prompt + "\n\nAdditional knowledge from library:\n" + lib_ctx
                    ).strip()
            except Exception as e:
                logger.warning(f"Failed to inject knowledge library for chatbot: {e}")

            # Generate response
            response_text, images, metadata = langchain_service.generate_response(
                chatbot_id=chatbot.id,
                query=message_content,
                conversation_history=history,
                system_prompt=effective_prompt,
            )

            return {
                "message": response_text or "I apologize, I couldn't generate a response.",
                "retrieved_documents": images,
                "token_usage": metadata.get("token_usage", {}) if isinstance(metadata, dict) else {},
            }

        except Exception as e:
            logger.error(f"Error generating bot response: {e}")
            return {
                "message": "I apologize, I'm having trouble processing your request. Please try again.",
                "error": str(e)
            }

    def _resolve_outbound_target(
        self,
        session: ChatSession,
        team: Optional[AgentTeam] = None,
    ) -> Dict[str, Any]:
        """Resolve which channel instance + recipient to use for outbound replies.

        4-tier priority chain:
        1. session.session_metadata  (fast, works for non-funnel handlers)
        2. ContactRoutingState.routing_metadata  (canonical, works for ALL handlers)
        3. SupportTicket.ticket_metadata  (backward compat for human inbox)
        4. Handler linkage (chatbot.whatsapp_instance / team.whatsapp_instance)

        Returns dict with: instance_id, provider_id, provider_type, recipient,
        remote_jid_alt, customer_phone, source.
        """
        result: Dict[str, Any] = {
            "instance_id": None,
            "provider_id": None,
            "provider_type": None,
            "recipient": session.user_identifier,
            "remote_jid_alt": None,
            "customer_phone": None,
            "source": None,
        }

        # Tier 1: session_metadata
        meta = session.session_metadata or {}
        if meta.get("inbound_instance_id"):
            result["instance_id"] = meta["inbound_instance_id"]
            result["provider_id"] = meta.get("inbound_provider_id")
            result["provider_type"] = meta.get("inbound_provider_type")
            result["recipient"] = meta.get("remote_jid") or session.user_identifier
            result["remote_jid_alt"] = meta.get("remote_jid_alt")
            result["customer_phone"] = meta.get("customer_phone")
            result["source"] = "session_metadata"
            return result

        # Tier 2: ContactRoutingState.routing_metadata
        project_id = None
        if team:
            project_id = team.project_id
        elif session.chatbot:
            project_id = session.chatbot.project_id

        if project_id:
            state = (
                self.db.query(ContactRoutingState)
                .filter(
                    ContactRoutingState.project_id == project_id,
                    ContactRoutingState.contact_identifier == session.user_identifier,
                    ContactRoutingState.channel == session.channel,
                )
                .order_by(ContactRoutingState.handler_priority.desc())
                .first()
            )
            if state:
                rmeta = state.routing_metadata or {}
                if rmeta.get("inbound_instance_id"):
                    result["instance_id"] = rmeta["inbound_instance_id"]
                    result["provider_id"] = rmeta.get("inbound_provider_id")
                    result["provider_type"] = rmeta.get("inbound_provider_type")
                    result["recipient"] = rmeta.get("remote_jid") or session.user_identifier
                    result["remote_jid_alt"] = rmeta.get("remote_jid_alt")
                    result["customer_phone"] = rmeta.get("customer_phone")
                    result["source"] = "routing_state"
                    return result

        # Tier 3: SupportTicket.ticket_metadata
        ticket = self.db.query(SupportTicket).filter(
            SupportTicket.session_id == session.id,
        ).first()
        if ticket:
            tmeta = ticket.ticket_metadata or {}
            if tmeta.get("instance_id"):
                result["instance_id"] = tmeta["instance_id"]
                result["provider_id"] = tmeta.get("provider_id")
                result["provider_type"] = tmeta.get("provider_type")
                result["recipient"] = (
                    tmeta.get("remote_jid")
                    or ticket.customer_phone
                    or session.user_identifier
                )
                result["remote_jid_alt"] = tmeta.get("remote_jid_alt")
                result["customer_phone"] = ticket.customer_phone
                result["source"] = "ticket_metadata"
                return result

        # Tier 4: Handler linkage fallback (via handler_channel_links first, then legacy FK)
        from app.services.handler_channel_link_service import HandlerChannelLinkService
        link_svc = HandlerChannelLinkService(self.db)

        # Messenger/Instagram: instance is a MetaPageConnection linked per channel
        if session.channel in ("messenger", "instagram"):
            handler_type, handler_id = None, None
            if team:
                handler_type, handler_id = "agent_team", team.id
            elif session.chatbot:
                handler_type, handler_id = "chatbot", session.chatbot.id
            if handler_type:
                meta_link = link_svc.get_link(handler_type, handler_id, session.channel)
                if meta_link and meta_link.instance_id:
                    result["instance_id"] = meta_link.instance_id
                    result["provider_type"] = "meta_graph"
                    result["source"] = f"{handler_type}_linkage"
                    return result

        if team:
            wa_link = link_svc.get_link("agent_team", team.id, "whatsapp")
            if wa_link and wa_link.instance_id:
                from app.models import WhatsAppInstance as WAI
                inst = self.db.query(WAI).filter(WAI.id == wa_link.instance_id).first()
                if inst:
                    result["instance_id"] = inst.id
                    result["provider_type"] = getattr(inst, "provider_type", "evolution_api")
                    result["source"] = "team_linkage"
                    return result
            # Legacy FK fallback
            if team.whatsapp_instance:
                result["instance_id"] = team.whatsapp_instance.id
                result["provider_type"] = getattr(team.whatsapp_instance, "provider_type", "evolution_api")
                result["source"] = "team_linkage"
                return result

        if session.chatbot:
            cb_link = link_svc.get_link("chatbot", session.chatbot.id, "whatsapp")
            if cb_link and cb_link.instance_id:
                from app.models import WhatsAppInstance as WAI
                inst = self.db.query(WAI).filter(WAI.id == cb_link.instance_id).first()
                if inst:
                    result["instance_id"] = inst.id
                    result["provider_type"] = getattr(inst, "provider_type", "evolution_api")
                    result["source"] = "chatbot_linkage"
                    return result
            # Legacy FK fallback
            if session.chatbot.whatsapp_instance:
                result["instance_id"] = session.chatbot.whatsapp_instance.id
                result["provider_type"] = getattr(session.chatbot.whatsapp_instance, "provider_type", "evolution_api")
            result["source"] = "chatbot_linkage"
            return result

        result["source"] = "none"
        return result

    async def _send_outgoing_message(
        self,
        session: ChatSession,
        provider: Optional[MessagingProvider],
        content: str,
        media_url: Optional[str] = None,
        media_type: Optional[str] = None,
        filename: Optional[str] = None,
        channel_override: Optional[str] = None,
        sender_type: str = "bot",
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Send a message through the appropriate channel.

        Uses _resolve_outbound_target() to route replies through the same
        channel/instance the customer used inbound.  When channel_override is
        set (omnichannel reply), the message is sent via the specified channel
        instead.
        """
        import os as _os

        channel = channel_override or session.channel
        target = self._resolve_outbound_target(session)
        has_media = bool(media_url and media_type)

        if channel == "web":
            return {"success": True, "channel": "web"}

        try:
            from app.services.channels.base import OutboundContent
            from app.services.channels.send_service import SendService

            if has_media:
                out_content = OutboundContent(
                    content_type="media",
                    media_url=media_url,
                    media_type=media_type,
                    media_caption=content,
                )
            else:
                out_content = OutboundContent(content_type="text", text=content)

            if attachments and channel == "email":
                out_content.attachments = attachments

            # Human replies on Messenger/Instagram may use the HUMAN_AGENT tag
            # (7-day window) when the standard 24h window has closed.
            if sender_type == "human_agent" and channel in ("messenger", "instagram"):
                out_content.metadata["human_agent"] = True

            instance_config = None
            if target["instance_id"]:
                instance_config = {"instance_id": target["instance_id"]}

            svc = SendService(self.db)
            _project_id = session.chatbot.project_id if session.chatbot else None
            decision = await svc.send(
                project_id=_project_id,
                user_id=None,
                recipient=target["recipient"],
                content=out_content,
                channel=channel,
                source_type="chatbot",
                source_id=session.id,
                instance_config=instance_config,
            )
            return {
                "success": decision.success,
                "send_log_id": decision.send_log_id,
                "channel": decision.channel_used,
                "error": decision.error,
                "message_id": decision.provider_message_id,
            }
        except Exception as e:
            logger.error(f"SendService chatbot error: {e}")
            return {"success": False, "error": str(e)}

    async def _notify_support_inbox(
        self,
        session: ChatSession,
        message: ChatMessage
    ):
        """Notify support inbox about a new message (placeholder for WebSocket)."""
        # This would typically emit a WebSocket event
        # For now, just log
        logger.info(f"New message in session {session.id} for support inbox")

    def _generate_ticket_number(self, project_id: int) -> str:
        """Generate a unique ticket number."""
        # Count existing tickets for this project
        count = self.db.query(SupportTicket).filter(
            SupportTicket.project_id == project_id
        ).count()

        return f"PROJ{project_id}-{count + 1:04d}"


def get_unified_chat_service(db: Session) -> UnifiedChatService:
    """Factory function to get UnifiedChatService instance."""
    return UnifiedChatService(db)
