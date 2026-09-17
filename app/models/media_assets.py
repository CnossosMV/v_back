"""
ProjectMediaAsset model — shareable media files and links for chatbots & agent teams.

Assets are injected into the LLM system prompt as a catalog. The LLM references
them with [ASSET:slug] tokens which are post-processed into actual media sends.
"""

from sqlalchemy import (
    Column, Integer, String, DateTime, Text, Boolean, ForeignKey,
    ARRAY, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class ProjectMediaAsset(Base):
    __tablename__ = "project_media_assets"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)

    # Scoping — both null = available to all in project
    chatbot_id = Column(Integer, ForeignKey("chatbots.id", ondelete="CASCADE"), nullable=True, index=True)
    specialist_id = Column(Integer, ForeignKey("specialist_agents.id", ondelete="CASCADE"), nullable=True, index=True)

    # Asset info
    label = Column(String(200), nullable=False)
    slug = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)  # For the LLM: when/why to share this

    # Media
    media_type = Column(String(30), nullable=False)  # image, video, audio, document, link
    media_url = Column(String(1000), nullable=True)   # External URL or backend-served URL
    file_path = Column(String(500), nullable=True)     # Local path for uploaded files
    file_name = Column(String(200), nullable=True)     # Original filename
    mime_type = Column(String(100), nullable=True)

    # Metadata
    tags = Column(ARRAY(String), default=[], nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    project = relationship("Project", back_populates="media_assets")
    chatbot = relationship("Chatbot", back_populates="media_assets")
    specialist = relationship("SpecialistAgent", back_populates="media_assets")

    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_media_asset_project_slug"),
        Index("ix_media_asset_scope", "project_id", "chatbot_id", "specialist_id"),
    )
