"""Campaign-facing channel registry.

Campaigns store provider-neutral recipients and profiles.  Adapters translate a
profile/sender identity into the existing Send Layer contract at dispatch time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.campaigns import CampaignAction, CampaignVariant, ChannelDeliveryProfile
from app.models.messaging import MessagingTemplate, MessagingUser
from app.services.channels.base import OutboundContent


@dataclass
class CampaignDispatch:
    recipient: str
    content: OutboundContent
    instance_config: dict[str, Any]
    template_id: int | None = None
    errors: list[str] = field(default_factory=list)


class CampaignChannelAdapter(ABC):
    channel: str
    endpoint_types: tuple[str, ...]

    @abstractmethod
    def build_dispatch(
        self,
        db: Session,
        profile: ChannelDeliveryProfile,
        action: CampaignAction,
        variant: CampaignVariant | None,
        user: MessagingUser,
        endpoint_value: str,
    ) -> CampaignDispatch:
        raise NotImplementedError


def _contact_variables(user: MessagingUser) -> dict[str, Any]:
    contact = {
        "id": user.id,
        "external_id": user.external_id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone_e164 or user.phone,
        "locale": user.locale,
        "timezone": user.timezone,
        "lifecycle_stage": user.lifecycle_stage,
        "segment": user.segment_name,
        **(user.properties or {}),
    }
    return {**contact, "user": contact, "contact": contact}


class EmailCampaignAdapter(CampaignChannelAdapter):
    channel = "email"
    endpoint_types = ("email",)

    def build_dispatch(
        self,
        db: Session,
        profile: ChannelDeliveryProfile,
        action: CampaignAction,
        variant: CampaignVariant | None,
        user: MessagingUser,
        endpoint_value: str,
    ) -> CampaignDispatch:
        config = dict(profile.config or {})
        sender = profile.sender_identity
        if sender:
            if sender.external_instance_id and not config.get("instance_id"):
                try:
                    config["instance_id"] = int(sender.external_instance_id)
                except (TypeError, ValueError):
                    # Non-numeric provider ids remain available to custom/API
                    # email implementations via an explicit profile config.
                    config.setdefault("external_instance_id", sender.external_instance_id)
            if sender.address:
                config.setdefault("from_email", sender.address)
            if sender.display_name:
                config.setdefault("from_name", sender.display_name)
            if sender.reply_to:
                config.setdefault("reply_to", sender.reply_to)
        config["user_id"] = user.id
        config["delivery_profile_id"] = profile.id
        config["provider"] = profile.provider

        action_config = dict(action.config or {})
        template_id = variant.template_id if variant and variant.template_id else action_config.get("template_id")
        body = variant.body if variant and variant.body is not None else action_config.get("body", "")
        subject = variant.subject if variant and variant.subject is not None else action_config.get("subject")
        body_format = action_config.get("body_format", "html")
        if template_id:
            template = db.query(MessagingTemplate).filter(
                MessagingTemplate.id == int(template_id),
                MessagingTemplate.project_id == user.project_id,
                MessagingTemplate.is_active == True,  # noqa: E712
            ).first()
            if not template:
                return CampaignDispatch(endpoint_value, OutboundContent(), config, errors=["template_not_found"])
            body = template.body
            subject = template.subject
            body_format = template.body_format or "html"
            template_id = template.id

        from app.services.messaging.template_renderer import template_renderer

        rendered_body, rendered_subject, _, missing = template_renderer.render_template(
            body or "",
            _contact_variables(user),
            subject,
            locale=user.locale,
            timezone=user.timezone,
        )
        if missing and not action_config.get("allow_missing_variables", False):
            return CampaignDispatch(
                endpoint_value,
                OutboundContent(),
                config,
                template_id=template_id,
                errors=[f"missing_template_variables:{','.join(sorted(set(missing)))}"],
            )
        if not rendered_body:
            return CampaignDispatch(endpoint_value, OutboundContent(), config, template_id, ["empty_content"])
        content = OutboundContent(
            content_type="text" if body_format == "plain" else "rich",
            text=rendered_body if body_format == "plain" else None,
            html=rendered_body if body_format != "plain" else None,
            subject=rendered_subject,
            metadata={
                "campaign_id": action.campaign_id,
                "campaign_action_id": action.id,
                "campaign_variant_id": variant.id if variant else None,
            },
        )
        return CampaignDispatch(endpoint_value, content, config, template_id=template_id)


class CampaignChannelRegistry:
    _adapters: dict[str, CampaignChannelAdapter] = {}

    @classmethod
    def register(cls, adapter: CampaignChannelAdapter) -> None:
        cls._adapters[adapter.channel] = adapter

    @classmethod
    def get(cls, channel: str) -> CampaignChannelAdapter | None:
        return cls._adapters.get(channel)

    @classmethod
    def channels(cls) -> list[str]:
        return sorted(cls._adapters)


CampaignChannelRegistry.register(EmailCampaignAdapter())
