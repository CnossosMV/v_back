"""
Meta Comment Service — Facebook Page + Instagram comments as inbox tickets.

Inbound comments (FB `feed` webhook field, IG `comments` field) become
SupportTickets on channels `messenger_comment` / `instagram_comment` —
one ticket/session per top-level comment thread. Comments never touch
ContactRoutingState (they are not DM conversations).

Replies from the inbox can be:
- public  → reply comment under the post (FB /{comment_id}/comments,
            IG /{ig_comment_id}/replies)
- private → DM to the commenter (POST /{page_id}/messages with
            recipient.comment_id) — ONE per comment, 7-day window.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import (
    ChatMessage,
    ChatSession,
    MetaPageConnection,
    SupportTicket,
)
from app.models.messaging import MessagingEvent
from app.services.encryption_service import decrypt_value as _decrypt

logger = logging.getLogger(__name__)

PRIVATE_REPLY_WINDOW_DAYS = 7

COMMENT_CHANNELS = ("messenger_comment", "instagram_comment")


class MetaCommentService:
    def __init__(self, db: Session):
        self.db = db

    # ── Inbound ──────────────────────────────────────────────────────────

    async def process_fb_comment(self, connection: MetaPageConnection, value: dict) -> Optional[dict]:
        """Handle a FB `feed` change with item == "comment"."""
        from_obj = value.get("from") or {}
        commenter_id = str(from_obj.get("id", ""))

        # Skip our own page's comments (incl. our public replies) — loop prevention
        if not commenter_id or commenter_id == connection.page_id:
            return None

        verb = value.get("verb", "add")
        created_time = value.get("created_time")
        return await self._ingest_comment(
            connection=connection,
            platform="messenger",
            channel="messenger_comment",
            commenter_id=commenter_id,
            commenter_name=from_obj.get("name"),
            comment_id=str(value.get("comment_id", "")),
            parent_id=str(value.get("parent_id") or ""),
            post_id=str(value.get("post_id") or ""),
            text=value.get("message", ""),
            verb=verb,
            permalink=value.get("permalink_url"),
            created_at=(
                datetime.utcfromtimestamp(int(created_time))
                if created_time else datetime.utcnow()
            ),
        )

    async def process_ig_comment(self, connection: MetaPageConnection, value: dict) -> Optional[dict]:
        """Handle an IG `comments` webhook change."""
        from_obj = value.get("from") or {}
        commenter_id = str(from_obj.get("id", ""))

        # Skip comments authored by the connected IG account — loop prevention
        if not commenter_id or commenter_id == connection.ig_account_id:
            return None

        media = value.get("media") or {}
        return await self._ingest_comment(
            connection=connection,
            platform="instagram",
            channel="instagram_comment",
            commenter_id=commenter_id,
            commenter_name=from_obj.get("username"),
            comment_id=str(value.get("id", "")),
            parent_id=str(value.get("parent_id") or ""),
            post_id=str(media.get("id") or ""),
            text=value.get("text", ""),
            verb="add",
            permalink=None,
            created_at=datetime.utcnow(),
        )

    async def _ingest_comment(
        self,
        connection: MetaPageConnection,
        platform: str,
        channel: str,
        commenter_id: str,
        commenter_name: Optional[str],
        comment_id: str,
        parent_id: str,
        post_id: str,
        text: str,
        verb: str,
        permalink: Optional[str],
        created_at: datetime,
    ) -> Optional[dict]:
        if not comment_id:
            return None
        if verb not in ("add", "edited", "remove"):
            return None

        project_id = connection.project_id

        # Thread anchor = top-level comment (replies carry parent_id)
        thread_id = parent_id if parent_id and parent_id != post_id else comment_id
        thread_key = f"{channel}:{thread_id}"

        # Resolve/attach contact (merges with DM identity when it exists —
        # FB commenter ids are page-scoped like PSIDs)
        contact = None
        try:
            from app.services.messaging.identity_resolver import identity_resolver
            contact = identity_resolver.resolve_inbound_contact(
                self.db, project_id, platform, commenter_id,
                channel_instance_id=connection.id,
                push_name=commenter_name,
            )
        except Exception as e:
            logger.warning(f"Comment contact resolution failed: {e}")

        # Find or create the thread session
        from app.services.support_inbox_service import SupportInboxService
        inbox_svc = SupportInboxService(self.db)

        session = (
            self.db.query(ChatSession)
            .filter(
                ChatSession.external_conversation_id == thread_key,
                ChatSession.channel == channel,
            )
            .first()
        )
        if not session:
            if verb != "add":
                return None  # ignore edits/removals of threads we never tracked
            bot = inbox_svc._get_or_create_inbox_system_chatbot(project_id)
            session = ChatSession(
                chatbot_id=bot.id,
                user_identifier=commenter_id,
                channel=channel,
                is_active=True,
                handler_type="human",
                human_takeover=True,
                human_takeover_at=datetime.utcnow(),
                contact_id=contact.id if contact else None,
                external_conversation_id=thread_key,
                session_metadata={
                    "inbound_instance_id": connection.id,
                    "platform": platform,
                    "page_id": connection.page_id,
                    "ig_account_id": connection.ig_account_id,
                },
            )
            self.db.add(session)
            self.db.flush()
        elif contact and not session.contact_id:
            session.contact_id = contact.id

        # Dedup by comment id (Meta retries webhooks)
        existing_msg = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == session.id,
                ChatMessage.external_message_id == comment_id,
            )
            .first()
        )
        if existing_msg and verb == "add":
            return {"success": True, "deduplicated": True}

        if verb == "add":
            chat_msg = ChatMessage(
                session_id=session.id,
                role="user",
                content=text or "[Comment]",
                sender_type="customer",
                external_message_id=comment_id,
                message_metadata={
                    "comment_id": comment_id,
                    "parent_id": parent_id or None,
                    "post_id": post_id or None,
                    "permalink": permalink,
                    "commenter_name": commenter_name,
                },
            )
        else:
            action = "edited their comment" if verb == "edited" else "deleted their comment"
            chat_msg = ChatMessage(
                session_id=session.id,
                role="system",
                content=f"{commenter_name or 'Customer'} {action}"
                + (f": {text}" if verb == "edited" and text else ""),
                sender_type="customer",
                message_metadata={"comment_id": comment_id, "verb": verb},
            )
        self.db.add(chat_msg)
        self.db.commit()

        # Ticket (one open ticket per thread session; create_ticket dedups)
        existing_ticket = self.db.query(SupportTicket).filter(
            SupportTicket.session_id == session.id,
            SupportTicket.status.in_(["open", "in_progress", "waiting_customer"]),
        ).first()
        is_followup = existing_ticket is not None

        ticket = inbox_svc.create_ticket(
            session_id=session.id,
            reason=f"{'Instagram' if platform == 'instagram' else 'Facebook'} comment",
            contact_id=contact.id if contact else None,
        )

        meta = dict(ticket.ticket_metadata or {})
        changed = False
        if not meta.get("instance_id"):
            meta.update({
                "instance_id": connection.id,
                "provider_type": "meta_graph",
                "platform": platform,
                "page_id": connection.page_id,
                "ig_account_id": connection.ig_account_id,
                "post_id": post_id or None,
                "comment_thread_id": thread_id,
                "permalink": permalink,
                "private_reply_sent": False,
                "thread_started_at": created_at.isoformat(),
            })
            changed = True
        if commenter_name and not ticket.customer_name:
            ticket.customer_name = commenter_name
            changed = True
        if changed:
            ticket.ticket_metadata = meta
            self.db.commit()

        # Realtime inbox notification
        try:
            from app.services.realtime.redis_pubsub import publish_inbox_event
            from app.services.realtime import event_types as rt

            event_data = {
                "ticket_id": ticket.id,
                "ticket_number": ticket.ticket_number,
                "message_id": chat_msg.id,
                "content": chat_msg.content,
                "sender_type": "customer",
                "channel": channel,
            }
            loop = asyncio.get_running_loop()
            loop.create_task(publish_inbox_event(project_id, rt.MESSAGE_RECEIVED, event_data))
        except RuntimeError:
            pass
        except Exception as e:
            logger.debug(f"Comment inbox notify skipped: {e}")

        # Messaging event (Event Actions / analytics can hook on this)
        try:
            event = MessagingEvent(
                project_id=project_id,
                user_id=contact.id if contact else None,
                event_name="comment_received",
                source="backend",
                properties={
                    "channel": channel,
                    "platform": platform,
                    "comment_id": comment_id,
                    "post_id": post_id or None,
                    "connection_id": connection.id,
                    "text": (text or "")[:500],
                },
            )
            self.db.add(event)
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            logger.debug(f"comment_received event skipped: {e}")

        logger.info(
            f"[META_COMMENT] {platform} comment {comment_id} → ticket "
            f"{ticket.ticket_number} (followup={is_followup})"
        )
        return {"success": True, "ticket_id": ticket.id, "session_id": session.id}

    # ── Outbound replies ─────────────────────────────────────────────────

    async def reply_to_comment_ticket(
        self,
        ticket: SupportTicket,
        mode: str,
        content: str,
        sent_by_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Public or private reply on a comment ticket."""
        from app.services.meta_messaging_service import (
            meta_messaging_service, mark_connection_error,
        )

        if ticket.channel not in COMMENT_CHANNELS:
            return {"success": False, "error": "Not a comment ticket"}
        if mode not in ("public", "private"):
            return {"success": False, "error": f"Invalid reply mode: {mode}"}

        meta = dict(ticket.ticket_metadata or {})
        connection = self.db.query(MetaPageConnection).filter(
            MetaPageConnection.id == meta.get("instance_id"),
        ).first()
        if not connection or not connection.page_access_token_enc:
            return {"success": False, "error": "Meta page connection not found or missing token"}

        page_token = _decrypt(connection.page_access_token_enc)
        platform = "instagram" if ticket.channel == "instagram_comment" else "messenger"

        # Target = latest customer comment in the thread (fallback: thread anchor)
        last_customer_msg = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == ticket.session_id,
                ChatMessage.role == "user",
                ChatMessage.external_message_id.isnot(None),
            )
            .order_by(ChatMessage.id.desc())
            .first()
        )
        target_comment_id = (
            last_customer_msg.external_message_id
            if last_customer_msg else meta.get("comment_thread_id")
        )
        if not target_comment_id:
            return {"success": False, "error": "No comment to reply to"}

        if mode == "private":
            if meta.get("private_reply_sent"):
                return {
                    "success": False,
                    "error": "Private reply already sent for this comment thread (Meta allows one)",
                }
            started = meta.get("thread_started_at")
            if started:
                try:
                    started_dt = datetime.fromisoformat(started)
                    if datetime.utcnow() - started_dt > timedelta(days=PRIVATE_REPLY_WINDOW_DAYS):
                        return {
                            "success": False,
                            "error": "Private reply window (7 days) has expired",
                        }
                except ValueError:
                    pass
            result = await meta_messaging_service.send_private_reply(
                connection.page_id, page_token, target_comment_id, content
            )
        else:
            if platform == "instagram":
                result = await meta_messaging_service.reply_to_ig_comment(
                    target_comment_id, page_token, content
                )
            else:
                result = await meta_messaging_service.reply_to_fb_comment(
                    target_comment_id, page_token, content
                )

        if not result.get("success"):
            mark_connection_error(self.db, connection, result)
            return {"success": False, "error": result.get("error")}

        # Store the agent reply
        chat_msg = ChatMessage(
            session_id=ticket.session_id,
            role="assistant",
            content=content,
            sender_type="human_agent",
            sent_by_user_id=sent_by_user_id,
            external_message_id=result.get("comment_id") or result.get("message_id"),
            delivery_status="sent",
            message_metadata={"reply_mode": mode, "target_comment_id": target_comment_id},
        )
        self.db.add(chat_msg)

        if mode == "private":
            meta["private_reply_sent"] = True
            ticket.ticket_metadata = meta
            system_msg = ChatMessage(
                session_id=ticket.session_id,
                role="system",
                content="Private reply sent — if the customer answers, the conversation continues in DMs.",
                sender_type="bot",
            )
            self.db.add(system_msg)

        ticket.first_response_at = ticket.first_response_at or datetime.utcnow()
        self.db.commit()
        self.db.refresh(chat_msg)

        return {
            "success": True,
            "message_id": chat_msg.id,
            "mode": mode,
            "provider_id": chat_msg.external_message_id,
        }
