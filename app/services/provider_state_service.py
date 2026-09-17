"""Provider-neutral operational state without credential exposure."""

from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.campaigns import CampaignRecipient, ChannelCapacityReservation, ChannelDeliveryProfile
from app.models.engine_control import ProviderOperationalState


class ProviderStateService:
    def __init__(self, db: Session): self.db = db

    def refresh(self, project_id: int) -> list[ProviderOperationalState]:
        now = datetime.utcnow(); day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        profiles = self.db.query(ChannelDeliveryProfile).filter(ChannelDeliveryProfile.project_id == project_id).all()
        for profile in profiles:
            used = self.db.query(func.coalesce(func.sum(ChannelCapacityReservation.units_consumed), 0)).filter(
                ChannelCapacityReservation.project_id == project_id,
                ChannelCapacityReservation.delivery_profile_id == profile.id,
                ChannelCapacityReservation.bucket_start >= day,
            ).scalar() or 0
            row = self.db.query(ProviderOperationalState).filter(
                ProviderOperationalState.project_id == project_id,
                ProviderOperationalState.profile_type == "delivery_profile",
                ProviderOperationalState.profile_id == str(profile.id),
            ).first()
            if not row:
                row = ProviderOperationalState(project_id=project_id, profile_type="delivery_profile", profile_id=str(profile.id))
                self.db.add(row)
            row.channel = profile.channel; row.provider = profile.provider
            row.status = "disabled" if profile.status != "active" else profile.health_status
            row.quota = {"per_minute": profile.max_per_minute, "per_hour": profile.max_per_hour, "per_day": profile.max_per_day, "concurrency": profile.concurrency_limit}
            row.usage = {"consumed_today": int(used)}
            row.capabilities = {"send": profile.status == "active", "health_check": True, "reserve_capacity": True}
            row.last_success_at = self.db.query(func.max(CampaignRecipient.sent_at)).filter(
                CampaignRecipient.project_id == project_id,
                CampaignRecipient.delivery_profile_id == profile.id,
                CampaignRecipient.status.in_(("sent", "delivered")),
            ).scalar()
            latest_error = self.db.query(CampaignRecipient).filter(
                CampaignRecipient.project_id == project_id,
                CampaignRecipient.delivery_profile_id == profile.id,
                CampaignRecipient.last_error_code.isnot(None),
            ).order_by(CampaignRecipient.updated_at.desc()).first()
            row.last_error_at = latest_error.updated_at if latest_error else None
            row.last_error_code = latest_error.last_error_code if latest_error else None
            row.source = "channel_delivery_profile"; row.observed_at = profile.last_health_check_at or now
            row.stale_after = (profile.last_health_check_at or now) + timedelta(minutes=15)
        self.db.commit()
        return self.list(project_id)

    def list(self, project_id: int) -> list[ProviderOperationalState]:
        return self.db.query(ProviderOperationalState).filter(ProviderOperationalState.project_id == project_id).order_by(ProviderOperationalState.channel, ProviderOperationalState.provider).all()

    @staticmethod
    def serialize(row: ProviderOperationalState) -> dict:
        now = datetime.utcnow()
        return {"id": row.id, "profile_type": row.profile_type, "profile_id": row.profile_id, "channel": row.channel, "provider": row.provider, "status": row.status, "quota": row.quota, "usage": row.usage, "capabilities": row.capabilities, "last_success_at": row.last_success_at, "last_error_at": row.last_error_at, "last_error_code": row.last_error_code, "source": row.source, "observed_at": row.observed_at, "stale_after": row.stale_after, "stale": row.stale_after <= now}
