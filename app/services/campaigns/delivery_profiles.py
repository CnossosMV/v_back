"""Sender identity and delivery-profile management."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.models import CustomerSMTPConfig, EmailInstance, Project
from app.models.campaigns import (
    ChannelCapacityReservation,
    ChannelDeliveryProfile,
    ChannelSenderIdentity,
)
from app.services.campaigns.channel_registry import CampaignChannelRegistry
from app.services.campaigns.capacity import lock_delivery_profile
from app.services.campaigns.recurrence import timezone_or_error


class DeliveryProfileError(ValueError):
    pass


class DeliveryProfileService:
    def __init__(self, db: Session):
        self.db = db

    def create_identity(self, project_id: int, values: dict[str, Any]) -> ChannelSenderIdentity:
        if CampaignChannelRegistry.get(str(values["channel"])) is None:
            raise DeliveryProfileError("Channel is not registered for campaigns")
        if values.get("is_default"):
            self._lock_identity_defaults(project_id, str(values["channel"]))
        row = ChannelSenderIdentity(
            project_id=project_id,
            channel=values["channel"],
            provider=values["provider"],
            identity_key=values["identity_key"],
            address=values.get("address"),
            display_name=values.get("display_name"),
            reply_to=values.get("reply_to"),
            external_instance_id=values.get("external_instance_id"),
            status="active",
            is_default=bool(values.get("is_default", False)),
            capabilities=values.get("capabilities"),
            metadata_=values.get("metadata"),
        )
        self.db.add(row)
        self.db.flush()
        if row.is_default:
            self._clear_other_defaults(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_identity(self, row: ChannelSenderIdentity, values: dict[str, Any]) -> ChannelSenderIdentity:
        profile_ids = [profile_id for (profile_id,) in self.db.query(
            ChannelDeliveryProfile.id,
        ).filter(
            ChannelDeliveryProfile.project_id == row.project_id,
            ChannelDeliveryProfile.sender_identity_id == row.id,
        ).order_by(ChannelDeliveryProfile.id).all()]
        for profile_id in profile_ids:
            lock_delivery_profile(self.db, row.project_id, profile_id)
        self.db.refresh(row)
        immutable_during_delivery = {
            "address", "display_name", "reply_to", "external_instance_id",
        }
        changed = {
            field for field in immutable_during_delivery
            if field in values and values[field] != getattr(row, field)
        }
        if changed and self._identity_has_active_reservations(row.id):
            raise DeliveryProfileError(
                "Sender identity delivery fields cannot change while campaign capacity is reserved"
            )
        if (
            values.get("status") == "disabled"
            and row.status != "disabled"
            and self._identity_has_active_reservations(row.id)
        ):
            raise DeliveryProfileError(
                "Sender identity cannot be disabled while campaign capacity is reserved"
            )
        if values.get("is_default") is True or (row.is_default and "is_default" not in values):
            self._lock_identity_defaults(row.project_id, row.channel)
        for field in (
            "address", "display_name", "reply_to", "external_instance_id",
            "status", "is_default", "capabilities",
        ):
            if field in values:
                setattr(row, field, values[field])
        if "metadata" in values:
            row.metadata_ = values["metadata"]
        if row.is_default:
            self._clear_other_defaults(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _clear_other_defaults(self, row: ChannelSenderIdentity) -> None:
        self.db.query(ChannelSenderIdentity).filter(
            ChannelSenderIdentity.project_id == row.project_id,
            ChannelSenderIdentity.channel == row.channel,
            ChannelSenderIdentity.id != row.id,
            ChannelSenderIdentity.is_default == True,  # noqa: E712
        ).update({ChannelSenderIdentity.is_default: False}, synchronize_session=False)

    def _lock_identity_defaults(self, project_id: int, channel: str) -> None:
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"campaign-sender-default:{project_id}:{channel}"},
            )

    def create_profile(self, project_id: int, values: dict[str, Any]) -> ChannelDeliveryProfile:
        values = dict(values)
        if not values.get("sender_identity_id"):
            identity = self._materialize_explicit_sender(project_id, values)
            if identity:
                values["sender_identity_id"] = identity.id
        self._validate_values(project_id, values)
        row = ChannelDeliveryProfile(
            project_id=project_id,
            channel=values["channel"],
            provider=values["provider"],
            name=values["name"],
            sender_identity_id=values.get("sender_identity_id"),
            max_per_minute=values.get("max_per_minute"),
            max_per_hour=values.get("max_per_hour"),
            max_per_day=values.get("max_per_day"),
            concurrency_limit=values.get("concurrency_limit"),
            priority=values.get("priority", 0),
            weight=values.get("weight", 100),
            timezone=values.get("timezone") or "UTC",
            warmup_config=values.get("warmup_config"),
            health_status="unknown",
            status="active",
            config=values.get("config") or {},
        )
        self.db.add(row)
        self.db.flush()
        self._check_health(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def available_senders(self, project_id: int) -> list[dict[str, Any]]:
        """List usable email senders without returning credentials or hosts."""
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise DeliveryProfileError("Project not found")
        result: list[dict[str, Any]] = []
        smtp_rows = self.db.query(CustomerSMTPConfig).filter(
            CustomerSMTPConfig.project_id == project_id,
            CustomerSMTPConfig.is_active == True,  # noqa: E712
        ).order_by(CustomerSMTPConfig.id).all()
        for row in smtp_rows:
            result.append({
                "kind": "smtp_config",
                "id": row.id,
                "provider": self._smtp_provider(row.smtp_server),
                "name": row.from_name or row.from_email,
                "address": row.from_email,
            })
        instance_rows = self.db.query(EmailInstance).filter(
            EmailInstance.workspace_id == project.workspace_id,
            EmailInstance.is_active == True,  # noqa: E712
            or_(EmailInstance.project_id.is_(None), EmailInstance.project_id == project_id),
            EmailInstance.connection_status == "verified",
        ).order_by(EmailInstance.id).all()
        for row in instance_rows:
            result.append({
                "kind": "email_instance",
                "id": row.id,
                "provider": (
                    self._smtp_provider(row.smtp_server)
                    if row.provider_type == "smtp"
                    else row.provider_type
                ),
                "name": row.instance_name,
                "address": row.from_email,
            })
        return result

    def _materialize_explicit_sender(
        self,
        project_id: int,
        values: dict[str, Any],
    ) -> ChannelSenderIdentity | None:
        """Bind an explicit legacy/instance sender without exposing secrets."""
        config = values.get("config") or {}
        if not isinstance(config, dict):
            raise DeliveryProfileError("config must be an object")
        instance_id = config.get("instance_id")
        smtp_config_id = config.get("smtp_config_id")
        if instance_id is not None and smtp_config_id is not None:
            raise DeliveryProfileError("Configure either instance_id or smtp_config_id, not both")
        if instance_id is None and smtp_config_id is None:
            return None
        if values.get("channel") != "email":
            raise DeliveryProfileError("Email sender configuration requires channel=email")
        requested_provider = str(values.get("provider") or "")
        if not requested_provider:
            raise DeliveryProfileError("provider is required")
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise DeliveryProfileError("Project not found")

        if instance_id is not None:
            instance_id = self._require_positive_int(instance_id, "config.instance_id")
            source = self.db.query(EmailInstance).filter(
                EmailInstance.id == instance_id,
                EmailInstance.workspace_id == project.workspace_id,
                EmailInstance.is_active == True,  # noqa: E712
                or_(EmailInstance.project_id.is_(None), EmailInstance.project_id == project_id),
                EmailInstance.connection_status == "verified",
            ).first()
            if not source:
                raise DeliveryProfileError("Email instance is unavailable for this project")
            provider = (
                self._smtp_provider(source.smtp_server)
                if source.provider_type == "smtp"
                else source.provider_type
            )
            values["provider"] = provider
            address = source.from_email
            display_name = source.from_name or source.instance_name
            external_instance_id = str(source.id)
            metadata = {"kind": "email_instance", "source_id": source.id}
        else:
            smtp_config_id = self._require_positive_int(
                smtp_config_id, "config.smtp_config_id",
            )
            source = self.db.query(CustomerSMTPConfig).filter(
                CustomerSMTPConfig.id == smtp_config_id,
                CustomerSMTPConfig.project_id == project_id,
                CustomerSMTPConfig.is_active == True,  # noqa: E712
            ).first()
            if not source:
                raise DeliveryProfileError("SMTP configuration is unavailable for this project")
            provider = self._smtp_provider(source.smtp_server)
            values["provider"] = provider
            address = source.from_email
            display_name = source.from_name or source.from_email
            external_instance_id = None
            metadata = {"kind": "smtp_config", "source_id": source.id}

        # Sender identities are revisions, not mutable aliases. A source
        # address/name change creates a new identity so scheduled and historic
        # campaign recipients keep the exact sender they were planned with.
        source_kind = str(metadata["kind"])
        source_id = int(metadata["source_id"])
        fingerprint = hashlib.sha256(
            f"{provider}\0{address or ''}\0{display_name or ''}\0{external_instance_id or ''}".encode()
        ).hexdigest()[:16]
        identity_key = f"{source_kind}:{source_id}:{provider}:{fingerprint}"

        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"campaign-sender:{project_id}:{identity_key}"},
            )
        identity = self.db.query(ChannelSenderIdentity).filter(
            ChannelSenderIdentity.project_id == project_id,
            ChannelSenderIdentity.channel == "email",
            ChannelSenderIdentity.identity_key == identity_key,
        ).first()
        if not identity:
            identity = ChannelSenderIdentity(
                project_id=project_id,
                channel="email",
                provider=provider,
                identity_key=identity_key,
                address=address,
                display_name=display_name,
                external_instance_id=external_instance_id,
                status="active",
                is_default=False,
                metadata_=metadata,
            )
            self.db.add(identity)
            self.db.flush()
        return identity

    @staticmethod
    def _smtp_provider(server: str | None) -> str:
        hostname = str(server or "").strip().lower()
        if "gmail" in hostname or "googlemail" in hostname:
            return "gmail"
        if "amazonaws" in hostname or "amazonses" in hostname:
            return "amazon_ses"
        if "office365" in hostname or "outlook" in hostname:
            return "microsoft_smtp"
        return "smtp"

    def update_profile(self, row: ChannelDeliveryProfile, values: dict[str, Any]) -> ChannelDeliveryProfile:
        lock_delivery_profile(self.db, row.project_id, row.id)
        self.db.refresh(row)
        immutable_during_delivery = {
            "sender_identity_id", "max_per_minute", "max_per_hour", "max_per_day",
            "concurrency_limit", "timezone", "warmup_config", "config",
        }
        changed = {
            field for field in immutable_during_delivery
            if field in values and values[field] != getattr(row, field)
        }
        if changed and self._profile_has_active_reservations(row.id):
            raise DeliveryProfileError(
                "Delivery profile routing and capacity cannot change while campaign capacity is reserved"
            )
        if (
            values.get("status") == "disabled"
            and row.status != "disabled"
            and self._profile_has_active_reservations(row.id)
        ):
            raise DeliveryProfileError(
                "Delivery profile cannot be disabled while campaign capacity is reserved"
            )
        merged = {
            "channel": row.channel,
            "provider": row.provider,
            "name": values.get("name", row.name),
            "sender_identity_id": values.get("sender_identity_id", row.sender_identity_id),
            "max_per_minute": values.get("max_per_minute", row.max_per_minute),
            "max_per_hour": values.get("max_per_hour", row.max_per_hour),
            "max_per_day": values.get("max_per_day", row.max_per_day),
            "concurrency_limit": values.get("concurrency_limit", row.concurrency_limit),
            "timezone": values.get("timezone", row.timezone),
            "config": values.get("config", row.config),
        }
        self._validate_values(row.project_id, merged)
        for field in (
            "name", "sender_identity_id", "max_per_minute", "max_per_hour",
            "max_per_day", "concurrency_limit", "priority", "weight", "timezone",
            "warmup_config", "status", "config",
        ):
            if field in values:
                setattr(row, field, values[field])
        if "sender_identity_id" in values:
            self.db.flush()
            self.db.expire(row, ["sender_identity"])
        # Health is derived exclusively from the bound provider credentials;
        # clients cannot promote a profile to healthy with an update payload.
        self._check_health(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _profile_has_active_reservations(self, profile_id: int) -> bool:
        return self.db.query(ChannelCapacityReservation.id).filter(
            ChannelCapacityReservation.delivery_profile_id == profile_id,
            ChannelCapacityReservation.status.in_(["reserved", "active"]),
        ).first() is not None

    def _identity_has_active_reservations(self, identity_id: int) -> bool:
        return self.db.query(ChannelCapacityReservation.id).join(
            ChannelDeliveryProfile,
            ChannelDeliveryProfile.id == ChannelCapacityReservation.delivery_profile_id,
        ).filter(
            ChannelDeliveryProfile.sender_identity_id == identity_id,
            ChannelCapacityReservation.status.in_(["reserved", "active"]),
        ).first() is not None

    def disable_profile(self, row: ChannelDeliveryProfile) -> ChannelDeliveryProfile:
        lock_delivery_profile(self.db, row.project_id, row.id)
        self.db.refresh(row)
        active = self.db.query(ChannelCapacityReservation.id).filter(
            ChannelCapacityReservation.delivery_profile_id == row.id,
            ChannelCapacityReservation.status.in_(["reserved", "active"]),
        ).first()
        if active:
            raise DeliveryProfileError("Profile has active campaign capacity reservations")
        row.status = "disabled"
        self.db.commit()
        self.db.refresh(row)
        return row

    def check_health(self, row: ChannelDeliveryProfile) -> dict[str, Any]:
        reason = self._check_health(row)
        self.db.commit()
        self.db.refresh(row)
        return {
            "id": row.id,
            "health_status": row.health_status,
            "checked_at": row.last_health_check_at,
            "reason": reason,
        }

    def _validate_values(self, project_id: int, values: dict[str, Any]) -> None:
        channel = str(values.get("channel") or "")
        provider = str(values.get("provider") or "")
        if CampaignChannelRegistry.get(channel) is None:
            raise DeliveryProfileError("Channel is not registered for campaigns")
        if not provider:
            raise DeliveryProfileError("provider is required")
        if not any(values.get(field) for field in ("max_per_minute", "max_per_hour", "max_per_day")):
            raise DeliveryProfileError("Configure at least one per-minute, hourly or daily capacity")
        timezone_or_error(values.get("timezone") or "UTC")
        config = values.get("config") or {}
        if not isinstance(config, dict):
            raise DeliveryProfileError("config must be an object")
        unknown_config = set(config) - {"instance_id", "smtp_config_id", "send_window"}
        if unknown_config:
            raise DeliveryProfileError(
                "Unsupported delivery-profile config fields: "
                + ", ".join(sorted(str(field) for field in unknown_config))
            )
        send_window = config.get("send_window")
        if send_window is not None:
            if not isinstance(send_window, dict) or set(send_window) - {"start", "end"}:
                raise DeliveryProfileError("config.send_window accepts only start and end")
            # Reuse capacity parsing so invalid clocks fail at configuration
            # time, not while planning the first campaign.
            from app.services.campaigns.capacity import _profile_window

            probe = type("ProfileWindowProbe", (), {
                "timezone": values.get("timezone") or "UTC",
                "config": {"send_window": send_window},
            })()
            try:
                _profile_window(probe, datetime.utcnow().date())
            except Exception as exc:
                raise DeliveryProfileError(str(exc)) from exc
        for field in ("instance_id", "smtp_config_id"):
            if field in config and config[field] is not None:
                self._require_positive_int(config[field], f"config.{field}")
        if config.get("instance_id") is not None and config.get("smtp_config_id") is not None:
            raise DeliveryProfileError("Configure either instance_id or smtp_config_id, not both")
        identity_id = values.get("sender_identity_id")
        if identity_id:
            identity = self.db.query(ChannelSenderIdentity).filter(
                ChannelSenderIdentity.id == identity_id,
                ChannelSenderIdentity.project_id == project_id,
                ChannelSenderIdentity.channel == channel,
                ChannelSenderIdentity.provider == provider,
                ChannelSenderIdentity.status == "active",
            ).first()
            if not identity:
                raise DeliveryProfileError("Sender identity does not match this project/channel/provider")
            if channel == "email":
                self._validate_email_sender_binding(project_id, identity, config)

    def _validate_email_sender_binding(
        self,
        project_id: int,
        identity: ChannelSenderIdentity,
        config: dict[str, Any],
    ) -> None:
        instance_id = config.get("instance_id")
        smtp_config_id = config.get("smtp_config_id")
        if instance_id is None and smtp_config_id is None:
            return
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise DeliveryProfileError("Project not found")
        if instance_id is not None:
            source = self.db.query(EmailInstance).filter(
                EmailInstance.id == instance_id,
                EmailInstance.workspace_id == project.workspace_id,
                EmailInstance.is_active == True,  # noqa: E712
                or_(EmailInstance.project_id.is_(None), EmailInstance.project_id == project_id),
                EmailInstance.connection_status == "verified",
            ).first()
            if not source:
                raise DeliveryProfileError("Email instance is unavailable for this project")
            provider = (
                self._smtp_provider(source.smtp_server)
                if source.provider_type == "smtp"
                else source.provider_type
            )
            address = source.from_email
        else:
            source = self.db.query(CustomerSMTPConfig).filter(
                CustomerSMTPConfig.id == smtp_config_id,
                CustomerSMTPConfig.project_id == project_id,
                CustomerSMTPConfig.is_active == True,  # noqa: E712
            ).first()
            if not source:
                raise DeliveryProfileError("SMTP configuration is unavailable for this project")
            provider = self._smtp_provider(source.smtp_server)
            address = source.from_email
        if identity.provider != provider:
            raise DeliveryProfileError("Sender identity provider does not match its configured source")
        if str(identity.address or "").strip().lower() != str(address or "").strip().lower():
            raise DeliveryProfileError("Sender identity address does not match its configured source")

    @staticmethod
    def _require_positive_int(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DeliveryProfileError(f"{field} must be a positive integer")
        return value

    def _check_health(self, row: ChannelDeliveryProfile) -> str | None:
        row.last_health_check_at = datetime.utcnow()
        if row.status != "active":
            row.health_status = "down"
            return "profile_not_active"
        sender = row.sender_identity
        if not sender or sender.status != "active":
            row.health_status = "down"
            return "sender_identity_missing_or_inactive"
        if row.channel != "email":
            row.health_status = "unknown"
            return "channel_health_adapter_not_implemented"

        config = dict(row.config or {})
        instance_id = config.get("instance_id")
        if instance_id is not None and (
            isinstance(instance_id, bool)
            or not isinstance(instance_id, int)
            or instance_id <= 0
        ):
            row.health_status = "down"
            return "invalid_email_instance_id"
        if not instance_id and sender.external_instance_id:
            try:
                instance_id = int(sender.external_instance_id)
            except (TypeError, ValueError):
                instance_id = None
            if instance_id is not None and instance_id <= 0:
                instance_id = None
        project = self.db.query(Project).filter(Project.id == row.project_id).first()
        if instance_id and project:
            instance = self.db.query(EmailInstance).filter(
                EmailInstance.id == int(instance_id),
                EmailInstance.workspace_id == project.workspace_id,
                EmailInstance.is_active == True,  # noqa: E712
            ).first()
            if instance and instance.project_id not in {None, row.project_id}:
                instance = None
            if not instance:
                row.health_status = "down"
                return "email_instance_missing"
            if instance.connection_status != "verified":
                row.health_status = "failed" if instance.connection_status == "failed" else "down"
                return "email_instance_not_verified"
            row.health_status = "healthy"
            return None

        smtp_config_id = config.get("smtp_config_id")
        if smtp_config_id is not None and (
            isinstance(smtp_config_id, bool)
            or not isinstance(smtp_config_id, int)
            or smtp_config_id <= 0
        ):
            row.health_status = "down"
            return "invalid_smtp_config_id"
        if smtp_config_id:
            smtp = self.db.query(CustomerSMTPConfig).filter(
                CustomerSMTPConfig.id == int(smtp_config_id),
                CustomerSMTPConfig.project_id == row.project_id,
                CustomerSMTPConfig.is_active == True,  # noqa: E712
            ).first()
            if smtp:
                row.health_status = "healthy"
                return None
            row.health_status = "down"
            return "smtp_config_missing"

        # Campaign delivery never silently selects another instance.
        row.health_status = "down"
        return "explicit_instance_config_required"
