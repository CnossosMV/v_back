"""
Channel Abstraction — base interface and data classes.

Every outbound channel implements ChannelAdapter.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class OutboundContent:
    """Normalised outbound payload (channel-agnostic)."""
    content_type: str = "text"  # text, template, media, rich
    text: Optional[str] = None
    html: Optional[str] = None
    subject: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None  # image, video, audio, document
    media_caption: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    template_components: Optional[List[Dict[str, Any]]] = None
    buttons: Optional[List[Dict[str, Any]]] = None
    # Email file attachments: list of {"url": str, "filename": str} dicts.
    attachments: Optional[List[Dict[str, Any]]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def summary(self, max_len: int = 500) -> str:
        """Return a truncated preview of the content."""
        preview = self.text or self.html or self.template_name or ""
        return preview[:max_len]

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for storage in SendLog.content_payload."""
        d: Dict[str, Any] = {"content_type": self.content_type}
        for attr in (
            "text", "html", "subject", "media_url", "media_type",
            "media_caption", "template_name", "template_language",
            "template_components", "buttons",
        ):
            val = getattr(self, attr)
            if val is not None:
                d[attr] = val
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OutboundContent":
        """Reconstruct from stored JSON."""
        return cls(
            content_type=d.get("content_type", "text"),
            text=d.get("text"),
            html=d.get("html"),
            subject=d.get("subject"),
            media_url=d.get("media_url"),
            media_type=d.get("media_type"),
            media_caption=d.get("media_caption"),
            template_name=d.get("template_name"),
            template_language=d.get("template_language"),
            template_components=d.get("template_components"),
            buttons=d.get("buttons"),
            metadata=d.get("metadata", {}),
        )


@dataclass
class SendResult:
    """Result of a single adapter.send() call."""
    success: bool
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    rate_limited: bool = False
    window_closed: bool = False
    provider_response: Optional[Dict[str, Any]] = None
    instance_id: Optional[int] = None


@dataclass
class ChannelConstraints:
    """Pre-flight check result for a channel."""
    available: bool = True
    session_window_open: Optional[bool] = None  # None = N/A
    opt_in_status: Optional[bool] = None
    rate_limited: bool = False
    quiet_hours_active: bool = False
    reason: Optional[str] = None


@dataclass
class SendDecision:
    """Final result from SendService.send()."""
    success: bool
    send_log_id: Optional[int] = None
    channel_used: Optional[str] = None
    status: str = "queued"
    error: Optional[str] = None
    decision_trace: List[Dict[str, Any]] = field(default_factory=list)
    provider_message_id: Optional[str] = None


# ── Abstract adapter ─────────────────────────────────────────────────────


class ChannelAdapter(ABC):
    """Interface every outbound channel must implement."""

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Canonical channel identifier (e.g. 'whatsapp', 'email')."""
        ...

    @abstractmethod
    async def send(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        content: OutboundContent,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver a message to a single recipient."""
        ...

    @abstractmethod
    def check_constraints(
        self,
        db: Session,
        project_id: int,
        recipient: str,
        instance_config: Optional[Dict[str, Any]] = None,
    ) -> ChannelConstraints:
        """Pre-flight constraint check (window, opt-in, rate limit, etc.)."""
        ...

    def negotiate_content(
        self,
        content: OutboundContent,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> OutboundContent:
        """Adapt content to channel capabilities. Default: passthrough."""
        return content
