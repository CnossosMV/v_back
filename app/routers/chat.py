"""
Chat Router

Handles chat interactions with chatbots using RAG.
"""

import os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Chatbot, ChatSession, ChatMessage
from app.schemas import (
    ChatRequest,
    ChatResponse,
    ChatSessionResponse,
    ChatMessageResponse,
)
from app.routers.auth import get_current_user

# Services
from app.services.chatbot.langchain_service import LangChainService
from app.services.chatbot.vector_store import VectorStoreService
from app.services.chatbot.knowledge_loader import KnowledgeLoader
from app.services.chatbot.conversation_manager import ConversationManager
from app.services.chatbot.llm_key_resolver import resolve_llm, resolve_embeddings, create_embeddings

router = APIRouter(tags=["chat"])


def get_chatbot_services(db: Session, project_id: int):
    """Initialize chatbot services using project-level LLM config."""
    try:
        emb_cfg = resolve_embeddings(db, project_id)
        chat_cfg = resolve_llm(db, project_id, purpose="chat")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    embeddings = create_embeddings(emb_cfg)
    vector_store_path = os.getenv("CHATBOT_VECTOR_STORE_PATH", "./vector_stores")
    knowledge_base_path = os.getenv("CHATBOT_KNOWLEDGE_BASE_PATH", "./knowledge_bases")

    # Initialize services
    knowledge_loader = KnowledgeLoader()
    vector_store = VectorStoreService(vector_store_path, embeddings)
    langchain_service = LangChainService(
        openai_api_key=chat_cfg.api_key,
        vector_store_service=vector_store,
        knowledge_loader=knowledge_loader
    )

    return langchain_service, knowledge_loader, knowledge_base_path, chat_cfg


@router.post("/chatbots/{chatbot_id}/chat", response_model=ChatResponse)
async def chat_with_bot(
    chatbot_id: int,
    chat_request: ChatRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Send a message to a chatbot and get a response.

    This endpoint:
    1. Creates or retrieves a chat session
    2. Retrieves relevant documents from the knowledge base
    3. Generates a response using RAG
    4. Stores the conversation in the database
    5. Returns the response with any relevant images
    """
    # Get chatbot
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    if chatbot.status != "active":
        raise HTTPException(status_code=400, detail="Chatbot is not active")

    # Initialize services using project-level LLM config
    try:
        langchain_service, knowledge_loader, knowledge_base_path, chat_cfg = get_chatbot_services(db, chatbot.project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Update LangChain service config from chatbot settings
    langchain_service.update_model_config(
        model_name=chatbot.model_name,
        temperature=chatbot.temperature,
        max_tokens=chatbot.max_tokens
    )

    # Get or create session
    conv_manager = ConversationManager(db)

    user_identifier = chat_request.user_identifier or current_user.email

    session = conv_manager.get_or_create_session(
        chatbot_id=chatbot_id,
        user_identifier=user_identifier,
        channel=chat_request.channel,
        user_id=current_user.id,
        context_data=chat_request.context
    )

    # Get conversation history
    conversation_history = conv_manager.get_conversation_history(
        session_id=session.id,
        limit=20
    )

    # Add user message
    user_message = conv_manager.add_message(
        session_id=session.id,
        role="user",
        content=chat_request.message
    )

    # Generate response using RAG
    try:
        response_text, images, metadata = langchain_service.generate_response(
            chatbot_id=chatbot_id,
            query=chat_request.message,
            conversation_history=conversation_history,
            system_prompt=chatbot.system_prompt,
            knowledge_base_path=chatbot.knowledge_base_path
        )

        # Add assistant message
        assistant_message = conv_manager.add_message(
            session_id=session.id,
            role="assistant",
            content=response_text,
            images=images,
            retrieved_documents=metadata.get("retrieved_documents", []),
            retrieval_metadata=metadata,
            token_usage=metadata.get("tokens_used", {}),
            message_metadata={"processing_time": metadata.get("processing_time", 0)}
        )

        # Return response
        return ChatResponse(
            message=response_text,
            session_id=session.id,
            images=images,
            retrieved_sources=metadata.get("retrieved_documents", []),
            tokens_used=metadata.get("tokens_used", {}).get("total_tokens"),
            processing_time=metadata.get("processing_time")
        )

    except Exception as e:
        # Log error and add system message
        conv_manager.add_message(
            session_id=session.id,
            role="system",
            content=f"Error generating response: {str(e)}",
            message_metadata={"error": True}
        )

        raise HTTPException(
            status_code=500,
            detail=f"Error generating response: {str(e)}"
        )


@router.get("/chatbots/{chatbot_id}/sessions", response_model=List[ChatSessionResponse])
async def get_sessions(
    chatbot_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get all sessions for a chatbot."""
    sessions = db.query(ChatSession).filter(
        ChatSession.chatbot_id == chatbot_id
    ).order_by(ChatSession.started_at.desc()).limit(limit).all()

    result = []
    for session in sessions:
        session_dict = ChatSessionResponse.from_orm(session).dict()

        # Add message count
        message_count = db.query(ChatMessage).filter(
            ChatMessage.session_id == session.id
        ).count()
        session_dict["message_count"] = message_count

        result.append(ChatSessionResponse(**session_dict))

    return result


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessageResponse])
async def get_session_messages(
    session_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get all messages for a session."""
    # Verify session exists
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = db.query(ChatMessage).filter(
        ChatMessage.session_id == session_id
    ).order_by(ChatMessage.timestamp.asc()).limit(limit).all()

    return [ChatMessageResponse.from_orm(msg) for msg in messages]


@router.post("/sessions/{session_id}/end", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """End a chat session."""
    conv_manager = ConversationManager(db)

    success = conv_manager.end_session(session_id)

    if not success:
        raise HTTPException(status_code=404, detail="Session not found")

    return None


@router.get("/sessions/{session_id}/stats")
async def get_session_stats(
    session_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get statistics for a session."""
    conv_manager = ConversationManager(db)

    stats = conv_manager.get_session_stats(session_id)

    if not stats:
        raise HTTPException(status_code=404, detail="Session not found")

    return stats


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Delete a session and all its messages."""
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Delete session (cascade will delete messages)
    db.delete(session)
    db.commit()

    return None
