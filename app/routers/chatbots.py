"""
Chatbots Router

CRUD endpoints for managing chatbots within projects.
"""

import os
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import Chatbot, Project, KnowledgeDocument, ChatSession, ChatMessage, ChatbotAgentRouting
from app.schemas import (
    ChatbotCreate,
    ChatbotUpdate,
    ChatbotResponse,
    KnowledgeDocumentResponse,
    ChatbotRoutingCreate,
    ChatbotRoutingUpdate,
    ChatbotRoutingResponse,
)
from app.routers.auth import get_current_user

router = APIRouter(tags=["chatbots"])


@router.get("/projects/{project_id}/chatbots", response_model=List[ChatbotResponse])
async def list_chatbots(
    project_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get all chatbots for a project."""
    # Verify project exists and user has access
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get chatbots
    chatbots = db.query(Chatbot).filter(
        Chatbot.project_id == project_id,
        Chatbot.status != "archived"
    ).all()

    # Enrich with statistics
    result = []
    for chatbot in chatbots:
        chatbot_dict = ChatbotResponse.from_orm(chatbot).dict()

        # Get counts
        chatbot_dict["total_sessions"] = db.query(ChatSession).filter(
            ChatSession.chatbot_id == chatbot.id
        ).count()

        chatbot_dict["total_messages"] = db.query(ChatMessage).join(ChatSession).filter(
            ChatSession.chatbot_id == chatbot.id
        ).count()

        chatbot_dict["total_documents"] = db.query(KnowledgeDocument).filter(
            KnowledgeDocument.chatbot_id == chatbot.id
        ).count()

        result.append(ChatbotResponse(**chatbot_dict))

    return result


@router.post("/projects/{project_id}/chatbots", response_model=ChatbotResponse, status_code=status.HTTP_201_CREATED)
async def create_chatbot(
    project_id: int,
    chatbot_data: ChatbotCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Create a new chatbot for a project."""
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Ensure project_id matches
    if chatbot_data.project_id != project_id:
        raise HTTPException(status_code=400, detail="Project ID mismatch")

    # Get environment paths
    vector_store_path = os.getenv("CHATBOT_VECTOR_STORE_PATH", "./vector_stores")
    knowledge_base_path = os.getenv("CHATBOT_KNOWLEDGE_BASE_PATH", "./knowledge_bases")

    # Create chatbot
    chatbot = Chatbot(
        **chatbot_data.dict(),
        vector_store_path=os.path.join(vector_store_path, str(project_id)),
        knowledge_base_path=os.path.join(knowledge_base_path, str(project_id))
    )

    db.add(chatbot)
    db.commit()
    db.refresh(chatbot)

    return ChatbotResponse.from_orm(chatbot)


@router.get("/chatbots/{chatbot_id}", response_model=ChatbotResponse)
async def get_chatbot(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get a specific chatbot by ID."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()

    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Enrich with statistics
    chatbot_dict = ChatbotResponse.from_orm(chatbot).dict()

    chatbot_dict["total_sessions"] = db.query(ChatSession).filter(
        ChatSession.chatbot_id == chatbot.id
    ).count()

    chatbot_dict["total_messages"] = db.query(ChatMessage).join(ChatSession).filter(
        ChatSession.chatbot_id == chatbot.id
    ).count()

    chatbot_dict["total_documents"] = db.query(KnowledgeDocument).filter(
        KnowledgeDocument.chatbot_id == chatbot.id
    ).count()

    return ChatbotResponse(**chatbot_dict)


@router.put("/chatbots/{chatbot_id}", response_model=ChatbotResponse)
async def update_chatbot(
    chatbot_id: int,
    chatbot_update: ChatbotUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Update a chatbot's configuration."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()

    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Update fields
    update_data = chatbot_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(chatbot, key, value)

    db.commit()
    db.refresh(chatbot)

    return ChatbotResponse.from_orm(chatbot)


@router.delete("/chatbots/{chatbot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chatbot(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Delete a chatbot and all associated data."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()

    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Delete vector store (static — no API key needed for file deletion)
    from app.services.chatbot.vector_store import VectorStoreService

    vector_store_path = os.getenv("CHATBOT_VECTOR_STORE_PATH", "./vector_stores")
    VectorStoreService.delete_vector_store_static(vector_store_path, chatbot_id)

    # Soft-delete: archive instead of hard-delete to preserve sessions, messages, and support tickets
    chatbot.status = "archived"
    db.commit()

    return None


@router.get("/chatbots/{chatbot_id}/knowledge", response_model=List[KnowledgeDocumentResponse])
async def list_knowledge_documents(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get all knowledge documents for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()

    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    documents = db.query(KnowledgeDocument).filter(
        KnowledgeDocument.chatbot_id == chatbot_id
    ).all()

    return [KnowledgeDocumentResponse.from_orm(doc) for doc in documents]


@router.get("/chatbots/{chatbot_id}/stats")
async def get_chatbot_stats(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get detailed statistics for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()

    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Use conversation manager for stats
    from app.services.chatbot.conversation_manager import ConversationManager

    conv_manager = ConversationManager(db)
    stats = conv_manager.get_chatbot_stats(chatbot_id)

    # Add knowledge base stats
    stats["total_documents"] = db.query(KnowledgeDocument).filter(
        KnowledgeDocument.chatbot_id == chatbot_id
    ).count()

    stats["processed_documents"] = db.query(KnowledgeDocument).filter(
        KnowledgeDocument.chatbot_id == chatbot_id,
        KnowledgeDocument.processed == True
    ).count()

    # Get vector store stats (static — no API key needed for reading counts)
    from app.services.chatbot.vector_store import VectorStoreService

    vector_store_path = os.getenv("CHATBOT_VECTOR_STORE_PATH", "./vector_stores")
    vector_stats = VectorStoreService.get_vector_store_stats_static(vector_store_path, chatbot_id)
    stats.update(vector_stats)

    return stats


# ============================================================================
# Chatbot Routing Configuration Endpoints
# ============================================================================

@router.get("/chatbots/{chatbot_id}/routing", response_model=ChatbotRoutingResponse)
async def get_chatbot_routing(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get routing configuration for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    routing = db.query(ChatbotAgentRouting).filter(
        ChatbotAgentRouting.chatbot_id == chatbot_id
    ).first()

    if not routing:
        raise HTTPException(status_code=404, detail="Routing configuration not found")

    return ChatbotRoutingResponse.from_orm(routing)


@router.post("/chatbots/{chatbot_id}/routing", response_model=ChatbotRoutingResponse, status_code=status.HTTP_201_CREATED)
async def create_chatbot_routing(
    chatbot_id: int,
    routing_data: ChatbotRoutingCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Create routing configuration for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    # Check if routing already exists
    existing = db.query(ChatbotAgentRouting).filter(
        ChatbotAgentRouting.chatbot_id == chatbot_id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Routing configuration already exists. Use PUT to update.")

    # Validate routing_mode
    valid_modes = ["human_only", "bot_only", "bot_then_human", "custom"]
    if routing_data.routing_mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid routing_mode. Must be one of: {', '.join(valid_modes)}"
        )

    routing = ChatbotAgentRouting(
        chatbot_id=chatbot_id,
        **routing_data.dict()
    )

    db.add(routing)
    db.commit()
    db.refresh(routing)

    return ChatbotRoutingResponse.from_orm(routing)


@router.put("/chatbots/{chatbot_id}/routing", response_model=ChatbotRoutingResponse)
async def update_chatbot_routing(
    chatbot_id: int,
    routing_update: ChatbotRoutingUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Update routing configuration for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    routing = db.query(ChatbotAgentRouting).filter(
        ChatbotAgentRouting.chatbot_id == chatbot_id
    ).first()

    if not routing:
        raise HTTPException(status_code=404, detail="Routing configuration not found. Use POST to create.")

    # Validate routing_mode if provided
    if routing_update.routing_mode:
        valid_modes = ["human_only", "bot_only", "bot_then_human", "custom"]
        if routing_update.routing_mode not in valid_modes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid routing_mode. Must be one of: {', '.join(valid_modes)}"
            )

    # Update fields
    update_data = routing_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(routing, key, value)

    db.commit()
    db.refresh(routing)

    return ChatbotRoutingResponse.from_orm(routing)


@router.delete("/chatbots/{chatbot_id}/routing", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chatbot_routing(
    chatbot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Delete routing configuration for a chatbot."""
    chatbot = db.query(Chatbot).filter(Chatbot.id == chatbot_id).first()
    if not chatbot:
        raise HTTPException(status_code=404, detail="Chatbot not found")

    routing = db.query(ChatbotAgentRouting).filter(
        ChatbotAgentRouting.chatbot_id == chatbot_id
    ).first()

    if not routing:
        raise HTTPException(status_code=404, detail="Routing configuration not found")

    db.delete(routing)
    db.commit()

    return None
