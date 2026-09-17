"""
Conversation Manager Service

Manages chat sessions, conversation history, and context for chatbots.
"""

import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from sqlalchemy.orm import Session

from app.models import ChatSession, ChatMessage, Chatbot, AgentTeam, SpecialistAgent
from app.schemas import ChatSessionCreate, ChatMessageCreate

logger = logging.getLogger(__name__)


class ConversationManager:
    """Service for managing chat sessions and conversation history."""

    def __init__(self, db: Session):
        """
        Initialize the conversation manager.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    def get_or_create_session(
        self,
        chatbot_id: int,
        user_identifier: str,
        channel: str = "web",
        user_id: Optional[int] = None,
        context_data: Optional[Dict] = None
    ) -> ChatSession:
        """
        Get an existing active session or create a new one.

        Args:
            chatbot_id: ID of the chatbot
            user_identifier: Unique identifier for the user (email, phone, anonymous ID)
            channel: Communication channel (web, whatsapp, api)
            user_id: Optional authenticated user ID
            context_data: Optional context data

        Returns:
            ChatSession object
        """
        # Try to find an active session
        session = self.db.query(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id,
            ChatSession.user_identifier == user_identifier,
            ChatSession.channel == channel,
            ChatSession.is_active == True
        ).first()

        if session:
            # Update last interaction time
            session.last_interaction_at = datetime.utcnow()
            self.db.commit()
            logger.info(f"Found existing session {session.id} for user {user_identifier}")
            return session

        # Create new session
        session = ChatSession(
            chatbot_id=chatbot_id,
            user_id=user_id,
            user_identifier=user_identifier,
            channel=channel,
            context_data=context_data or {},
            session_metadata={},
            is_active=True
        )

        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)

        logger.info(f"Created new session {session.id} for user {user_identifier}")
        return session

    def get_session_by_id(self, session_id: int) -> Optional[ChatSession]:
        """
        Get a session by ID.

        Args:
            session_id: ID of the session

        Returns:
            ChatSession object or None
        """
        return self.db.query(ChatSession).filter(ChatSession.id == session_id).first()

    def get_conversation_history(
        self,
        session_id: int,
        limit: int = 20
    ) -> List[Dict[str, str]]:
        """
        Get conversation history for a session.

        Args:
            session_id: ID of the session
            limit: Maximum number of messages to retrieve

        Returns:
            List of messages in format [{"role": "user", "content": "..."}, ...]
        """
        messages = self.db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id
        ).order_by(
            ChatMessage.timestamp.asc()
        ).limit(limit).all()

        history = []
        for msg in messages:
            history.append({
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat(),
                "images": msg.images or []
            })

        return history

    def add_message(
        self,
        session_id: int,
        role: str,
        content: str,
        images: Optional[List[str]] = None,
        retrieved_documents: Optional[List[Dict]] = None,
        retrieval_metadata: Optional[Dict] = None,
        token_usage: Optional[Dict] = None,
        message_metadata: Optional[Dict] = None
    ) -> ChatMessage:
        """
        Add a message to a session.

        Args:
            session_id: ID of the session
            role: Message role (user, assistant, system)
            content: Message content
            images: List of image paths/URLs
            retrieved_documents: Documents used for RAG
            retrieval_metadata: Metadata about retrieval
            token_usage: Token usage information
            message_metadata: Additional metadata

        Returns:
            ChatMessage object
        """
        # Extract token counts if provided
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None

        if token_usage:
            prompt_tokens = token_usage.get("prompt_tokens")
            completion_tokens = token_usage.get("completion_tokens")
            total_tokens = token_usage.get("total_tokens")

        message = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            images=images or [],
            retrieved_documents=retrieved_documents or [],
            retrieval_metadata=retrieval_metadata or {},
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            message_metadata=message_metadata or {}
        )

        self.db.add(message)

        # Update session's last interaction time
        session = self.get_session_by_id(session_id)
        if session:
            session.last_interaction_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(message)

        logger.info(f"Added {role} message to session {session_id}")
        return message

    def end_session(self, session_id: int) -> bool:
        """
        Mark a session as ended.

        Args:
            session_id: ID of the session

        Returns:
            True if successful
        """
        session = self.get_session_by_id(session_id)

        if not session:
            logger.warning(f"Session {session_id} not found")
            return False

        session.is_active = False
        session.ended_at = datetime.utcnow()

        self.db.commit()
        logger.info(f"Ended session {session_id}")
        return True

    def get_session_stats(self, session_id: int) -> Dict:
        """
        Get statistics for a session.

        Args:
            session_id: ID of the session

        Returns:
            Dictionary with session statistics
        """
        session = self.get_session_by_id(session_id)

        if not session:
            return {}

        message_count = self.db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id
        ).count()

        total_tokens = self.db.query(
            ChatMessage.total_tokens
        ).filter(
            ChatMessage.session_id == session_id,
            ChatMessage.total_tokens.isnot(None)
        ).all()

        total_token_count = sum(t[0] for t in total_tokens if t[0])

        return {
            "session_id": session_id,
            "message_count": message_count,
            "total_tokens_used": total_token_count,
            "started_at": session.started_at.isoformat(),
            "last_interaction_at": session.last_interaction_at.isoformat(),
            "is_active": session.is_active,
            "channel": session.channel,
        }

    def get_user_sessions(
        self,
        chatbot_id: int,
        user_identifier: str,
        limit: int = 10
    ) -> List[ChatSession]:
        """
        Get all sessions for a user across a chatbot.

        Args:
            chatbot_id: ID of the chatbot
            user_identifier: User identifier
            limit: Maximum number of sessions to retrieve

        Returns:
            List of ChatSession objects
        """
        sessions = self.db.query(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id,
            ChatSession.user_identifier == user_identifier
        ).order_by(
            ChatSession.started_at.desc()
        ).limit(limit).all()

        return sessions

    def get_chatbot_stats(self, chatbot_id: int) -> Dict:
        """
        Get statistics for a chatbot.

        Args:
            chatbot_id: ID of the chatbot

        Returns:
            Dictionary with chatbot statistics
        """
        total_sessions = self.db.query(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id
        ).count()

        active_sessions = self.db.query(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id,
            ChatSession.is_active == True
        ).count()

        # Get total messages across all sessions
        total_messages = self.db.query(ChatMessage).join(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id
        ).count()

        # Get total tokens used
        total_tokens = self.db.query(
            ChatMessage.total_tokens
        ).join(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id,
            ChatMessage.total_tokens.isnot(None)
        ).all()

        total_token_count = sum(t[0] for t in total_tokens if t[0])

        return {
            "chatbot_id": chatbot_id,
            "total_sessions": total_sessions,
            "active_sessions": active_sessions,
            "total_messages": total_messages,
            "total_tokens_used": total_token_count,
        }

    def cleanup_old_sessions(self, chatbot_id: int, days_old: int = 30) -> int:
        """
        Clean up old inactive sessions.

        Args:
            chatbot_id: ID of the chatbot
            days_old: Sessions older than this many days will be deleted

        Returns:
            Number of sessions deleted
        """
        from datetime import timedelta

        cutoff_date = datetime.utcnow() - timedelta(days=days_old)

        old_sessions = self.db.query(ChatSession).filter(
            ChatSession.chatbot_id == chatbot_id,
            ChatSession.is_active == False,
            ChatSession.ended_at < cutoff_date
        ).all()

        count = len(old_sessions)

        for session in old_sessions:
            self.db.delete(session)

        self.db.commit()
        logger.info(f"Cleaned up {count} old sessions for chatbot {chatbot_id}")

        return count

    # ========================================================================
    # Agent Teams Session Management
    # ========================================================================

    def get_or_create_team_session(
        self,
        team_id: int,
        user_identifier: str,
        channel: str = "web",
        user_id: Optional[int] = None,
        context_data: Optional[Dict] = None,
        chatbot_id: Optional[int] = None,
    ) -> ChatSession:
        """
        Get an existing active team session or create a new one.

        Args:
            team_id: ID of the agent team
            user_identifier: Unique identifier for the user
            channel: Communication channel
            user_id: Optional authenticated user ID
            context_data: Optional context data
            chatbot_id: Optional chatbot ID for backwards compatibility

        Returns:
            ChatSession object
        """
        # Try to find an active team session
        session = self.db.query(ChatSession).filter(
            ChatSession.team_id == team_id,
            ChatSession.user_identifier == user_identifier,
            ChatSession.channel == channel,
            ChatSession.is_active == True
        ).first()

        if session:
            session.last_interaction_at = datetime.utcnow()
            self.db.commit()
            logger.info(f"Found existing team session {session.id} for user {user_identifier}")
            return session

        # Create new team session
        # Use the first chatbot in the project as fallback for chatbot_id (required FK)
        if not chatbot_id:
            team = self.db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
            if team:
                chatbot = self.db.query(Chatbot).filter(
                    Chatbot.project_id == team.project_id
                ).first()
                chatbot_id = chatbot.id if chatbot else None

        session = ChatSession(
            chatbot_id=chatbot_id or 0,
            team_id=team_id,
            user_id=user_id,
            user_identifier=user_identifier,
            channel=channel,
            context_data=context_data or {},
            session_metadata={},
            session_state="INITIALIZED",
            is_active=True,
            routing_history=[],
        )

        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)

        logger.info(f"Created new team session {session.id} for team {team_id}")
        return session

    def update_session_state(self, session_id: int, state: str) -> None:
        """Update the session state."""
        session = self.get_session_by_id(session_id)
        if session:
            session.session_state = state
            session.last_interaction_at = datetime.utcnow()
            self.db.commit()

    def set_current_agent(self, session_id: int, agent_id: int) -> None:
        """Set the current specialist agent for a session."""
        session = self.get_session_by_id(session_id)
        if session:
            session.current_agent_id = agent_id
            session.last_interaction_at = datetime.utcnow()
            self.db.commit()

    def update_context_summary(
        self,
        session_id: int,
        summary: str,
        entities: Optional[Dict] = None
    ) -> None:
        """Update the rolling context summary and extracted entities."""
        session = self.get_session_by_id(session_id)
        if session:
            session.context_summary = summary
            if entities is not None:
                session.extracted_entities = entities
            self.db.commit()

    def add_team_message(
        self,
        session_id: int,
        role: str,
        content: str,
        agent_id: Optional[int] = None,
        routing_decision: Optional[Dict] = None,
        images: Optional[List[str]] = None,
        retrieved_documents: Optional[List[Dict]] = None,
        retrieval_metadata: Optional[Dict] = None,
        token_usage: Optional[Dict] = None,
        message_metadata: Optional[Dict] = None,
    ) -> ChatMessage:
        """
        Add a message to a team session, including agent and routing info.
        """
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None

        if token_usage:
            prompt_tokens = token_usage.get("prompt_tokens")
            completion_tokens = token_usage.get("completion_tokens")
            total_tokens = token_usage.get("total_tokens")

        message = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            agent_id=agent_id,
            routing_decision=routing_decision,
            images=images or [],
            retrieved_documents=retrieved_documents or [],
            retrieval_metadata=retrieval_metadata or {},
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            message_metadata=message_metadata or {},
        )

        self.db.add(message)

        # Update session's last interaction time
        session = self.get_session_by_id(session_id)
        if session:
            session.last_interaction_at = datetime.utcnow()
            # Append to routing history if there's a routing decision
            if routing_decision:
                history = session.routing_history or []
                history.append(routing_decision)
                session.routing_history = history

        self.db.commit()
        self.db.refresh(message)

        logger.info(f"Added {role} message to team session {session_id} (agent: {agent_id})")
        return message
