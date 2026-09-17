"""
ChannelRegistryService — project-scoped channel registry with availability checks.
"""
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import (
    ChannelCapability,
    ChatWidgetConfig,
    CustomerSMTPConfig,
    EmailInboundAddress,
    MessagingProvider,
    MetaPageConnection,
    Project,
    ProjectChannelConfig,
    WhatsAppInstance,
)

logger = logging.getLogger(__name__)


class ChannelRegistryService:
    def __init__(self, db: Session):
        self.db = db

    def get_registry(self, project_id: int) -> List[dict]:
        """All channels + capabilities + per-project availability."""
        caps = (
            self.db.query(ChannelCapability)
            .order_by(ChannelCapability.channel)
            .all()
        )
        project = self.db.query(Project).get(project_id)
        if not project:
            return []

        # Load project-level toggles
        enabled_map = self._get_enabled_map(project_id)

        result = []
        for cap in caps:
            channel_enabled = enabled_map.get(cap.channel, True)
            has_instances, count = self._check_instances(project, cap.channel)
            available = channel_enabled and has_instances
            result.append({
                "channel": cap.channel,
                "display_name": cap.display_name,
                "icon_hint": cap.icon_hint,
                "supported_statuses": cap.supported_statuses or [],
                "enabled": channel_enabled,
                "available": available,
                "instance_count": count,
                "instances": self._get_instances(project, cap.channel),
                "is_inbound_capable": cap.is_inbound_capable,
                "inbound_requires_setup": cap.inbound_requires_setup,
                "capabilities": {
                    "id": cap.id,
                    "channel": cap.channel,
                    "display_name": cap.display_name,
                    "max_text_length": cap.max_text_length,
                    "supports_media": cap.supports_media,
                    "supported_media_types": cap.supported_media_types,
                    "max_media_size_mb": cap.max_media_size_mb,
                    "supports_buttons": cap.supports_buttons,
                    "max_buttons": cap.max_buttons,
                    "supports_templates": cap.supports_templates,
                    "supports_rich_text": cap.supports_rich_text,
                    "supports_reactions": cap.supports_reactions,
                    "has_session_window": cap.has_session_window,
                    "session_window_hours": cap.session_window_hours,
                    "requires_opt_in": cap.requires_opt_in,
                    "supports_read_receipts": cap.supports_read_receipts,
                    "is_inbound_capable": cap.is_inbound_capable,
                    "inbound_requires_setup": cap.inbound_requires_setup,
                },
            })
        return result

    # ── Convenience helpers ───────────────────────────────────────────────

    def is_channel_available(self, project_id: int, channel: str) -> bool:
        """Quick check: is this channel both enabled and has instances?"""
        project = self.db.query(Project).get(project_id)
        if not project:
            return False
        enabled_map = self._get_enabled_map(project_id)
        if not enabled_map.get(channel, True):
            return False
        has_instances, _ = self._check_instances(project, channel)
        return has_instances

    def get_available_instances(self, project_id: int, channel: str) -> List[dict]:
        """Return instances only if channel is enabled."""
        project = self.db.query(Project).get(project_id)
        if not project:
            return []
        enabled_map = self._get_enabled_map(project_id)
        if not enabled_map.get(channel, True):
            return []
        return self._get_instances(project, channel)

    def get_default_instance(self, project_id: int, channel: str) -> Optional[dict]:
        """Return the first available instance for a channel, or None."""
        instances = self.get_available_instances(project_id, channel)
        return instances[0] if instances else None

    # ── Internal ──────────────────────────────────────────────────────────

    def _get_enabled_map(self, project_id: int) -> Dict[str, bool]:
        """Load project channel toggles into a dict."""
        rows = (
            self.db.query(ProjectChannelConfig.channel, ProjectChannelConfig.enabled)
            .filter(ProjectChannelConfig.project_id == project_id)
            .all()
        )
        return {ch: enabled for ch, enabled in rows}

    def _whatsapp_project_filter(self, project: Project):
        """Filter WhatsApp instances by project_id.

        The workspace fallback applies ONLY to legacy Evolution rows with a
        NULL project_id — meta_cloud_api instances are strictly project-scoped
        and must never appear in every project of a workspace.
        """
        return or_(
            WhatsAppInstance.project_id == project.id,
            and_(
                WhatsAppInstance.project_id.is_(None),
                WhatsAppInstance.workspace_id == project.workspace_id,
                WhatsAppInstance.provider_type == "evolution_api",
            ),
        )

    def _check_instances(self, project: Project, channel: str) -> Tuple[bool, int]:
        """Check if a channel has instances (regardless of enabled toggle)."""
        if channel == "whatsapp":
            count = (
                self.db.query(WhatsAppInstance)
                .filter(self._whatsapp_project_filter(project))
                .count()
            )
            return (count > 0, count)

        if channel == "email":
            count = (
                self.db.query(CustomerSMTPConfig)
                .filter(CustomerSMTPConfig.project_id == project.id)
                .count()
            )
            return (count > 0, count)

        if channel == "sms":
            count = (
                self.db.query(MessagingProvider)
                .filter(
                    MessagingProvider.project_id == project.id,
                    MessagingProvider.provider_type == "twilio_sms",
                    MessagingProvider.is_active == True,
                )
                .count()
            )
            return (count > 0, count)

        if channel == "web":
            count = (
                self.db.query(ChatWidgetConfig)
                .filter(
                    ChatWidgetConfig.project_id == project.id,
                    ChatWidgetConfig.is_active == True,
                )
                .count()
            )
            # Web is always considered to have at least 1 potential instance
            return (True, max(count, 1))

        if channel in ("messenger", "instagram"):
            count = (
                self.db.query(MetaPageConnection)
                .filter(
                    MetaPageConnection.project_id == project.id,
                    MetaPageConnection.is_active == True,
                    self._meta_platform_filter(channel),
                )
                .count()
            )
            return (count > 0, count)

        # inapp, push — not yet wired
        return (False, 0)

    @staticmethod
    def _meta_platform_filter(channel: str):
        return (
            MetaPageConnection.messenger_enabled == True
            if channel == "messenger"
            else MetaPageConnection.instagram_enabled == True
        )

    def _get_instances(self, project: Project, channel: str) -> List[dict]:
        """Return instance summaries for a channel within a project."""
        if channel == "whatsapp":
            insts = (
                self.db.query(WhatsAppInstance)
                .filter(
                    self._whatsapp_project_filter(project),
                    WhatsAppInstance.is_active == True,
                )
                .all()
            )
            return [
                {
                    "id": i.id,
                    "name": i.instance_name,
                    "provider_type": i.provider_type,
                    "status": i.connection_status,
                    "phone": i.phone_number,
                    "is_bidirectional": True,
                }
                for i in insts
            ]

        if channel == "email":
            cfgs = (
                self.db.query(CustomerSMTPConfig)
                .filter(
                    CustomerSMTPConfig.project_id == project.id,
                    CustomerSMTPConfig.is_active == True,
                )
                .all()
            )
            result = []
            for c in cfgs:
                from_domain = c.from_email.split("@")[1] if c.from_email and "@" in c.from_email else None
                addrs = (
                    self.db.query(EmailInboundAddress)
                    .filter(EmailInboundAddress.instance_id == c.id, EmailInboundAddress.is_active == True)
                    .all()
                )
                result.append({
                    "id": c.id,
                    "name": c.from_name or c.from_email or c.smtp_server,
                    "provider_type": "smtp",
                    "status": "connected",
                    "phone": None,
                    "is_bidirectional": c.is_bidirectional,
                    "from_domain": from_domain,
                    "inbound_addresses": [
                        {"id": a.id, "address": a.address, "label": a.label, "is_active": a.is_active}
                        for a in addrs
                    ],
                })
            return result

        if channel == "sms":
            provs = (
                self.db.query(MessagingProvider)
                .filter(
                    MessagingProvider.project_id == project.id,
                    MessagingProvider.provider_type == "twilio_sms",
                    MessagingProvider.is_active == True,
                )
                .all()
            )
            return [
                {
                    "id": p.id,
                    "name": p.name or p.phone_number,
                    "provider_type": p.provider_type,
                    "status": "connected" if p.is_verified else "pending",
                    "phone": p.phone_number,
                    "is_bidirectional": True,
                }
                for p in provs
            ]

        if channel == "web":
            widgets = (
                self.db.query(ChatWidgetConfig)
                .filter(
                    ChatWidgetConfig.project_id == project.id,
                    ChatWidgetConfig.is_active == True,
                )
                .all()
            )
            if widgets:
                return [
                    {
                        "id": w.id,
                        "name": w.bot_name or f"Widget {w.widget_key[:8]}",
                        "provider_type": "web_widget",
                        "status": "connected",
                        "phone": None,
                        "is_bidirectional": True,
                    }
                    for w in widgets
                ]
            # Fallback: no widgets yet but web is always potentially available
            return [
                {
                    "id": 0,
                    "name": "Web Chat",
                    "provider_type": "built_in",
                    "status": "connected",
                    "phone": None,
                    "is_bidirectional": True,
                }
            ]

        if channel in ("messenger", "instagram"):
            conns = (
                self.db.query(MetaPageConnection)
                .filter(
                    MetaPageConnection.project_id == project.id,
                    MetaPageConnection.is_active == True,
                    self._meta_platform_filter(channel),
                )
                .all()
            )
            return [
                {
                    "id": c.id,
                    "name": (
                        c.page_name
                        if channel == "messenger"
                        else (f"@{c.ig_username}" if c.ig_username else c.page_name)
                    ),
                    "provider_type": "meta_graph",
                    "status": c.status,
                    "phone": None,
                    "is_bidirectional": True,
                    "page_id": c.page_id,
                    "ig_account_id": c.ig_account_id,
                }
                for c in conns
            ]

        return []
