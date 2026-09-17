"""
Policy Service — Centralized automation guardrails.

Mediates all automation with contact caps, channel cooldowns,
quiet hours, and conflict resolution.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from app.models import ProjectPolicy, ContactLedger
from app.schemas.policies import PolicyDecision

logger = logging.getLogger(__name__)


class PolicyService:
    def __init__(self, db: Session, clock=None):
        self.db = db
        from app.services.clock import SystemClock
        self.clock = clock or SystemClock()

    def check_can_contact(
        self,
        project_id: int,
        user_id: int,
        channel: str,
        source: str,
        source_id: Optional[int] = None,
        *,
        ignored_policies: Optional[set[str]] = None,
    ) -> PolicyDecision:
        """
        Central gate: checks all policies before any message is sent.
        Returns PolicyDecision(allowed=bool, reason=str, policy_violated=str|None)
        """
        ignored = set(ignored_policies or set())
        allowed_ignored = {"quiet_hours", "channel_cooldown", "contact_caps"}
        invalid_ignored = ignored - allowed_ignored
        if invalid_ignored:
            raise ValueError(
                "Policy bypass is not allowed for: " + ", ".join(sorted(invalid_ignored))
            )

        policy = self._get_project_policy(project_id)
        if not policy or not policy.is_active:
            return PolicyDecision(allowed=True)

        # i18n: a contact's locale may override quiet hours, and quiet hours are
        # evaluated in the contact's own timezone. Flag-gated; off ⇒ project base only.
        override_quiet, contact_tz = self._locale_quiet_override(project_id, user_id)

        # 1. Check quiet hours (per-locale override + contact timezone when present)
        if "quiet_hours" not in ignored:
            quiet_check = self._check_quiet_hours(policy, channel, override_quiet, contact_tz)
            if not quiet_check.allowed:
                return quiet_check

        # 2. Check channel cooldown
        if "channel_cooldown" not in ignored:
            cooldown_check = self._check_channel_cooldown(policy, project_id, user_id, channel)
            if not cooldown_check.allowed:
                return cooldown_check

        # 3. Check contact caps
        if "contact_caps" not in ignored:
            cap_check = self._check_contact_caps(policy, project_id, user_id, channel)
            if not cap_check.allowed:
                return cap_check

        # 4. Check priority conflicts
        priority_check = self._check_priority(policy, project_id, user_id, source)
        if not priority_check.allowed:
            return priority_check

        return PolicyDecision(allowed=True)

    def record_contact(
        self,
        project_id: int,
        user_id: int,
        channel: str,
        source: str,
        source_id: Optional[int] = None,
        sent_at: Optional[datetime] = None,
    ) -> None:
        """Record a contact in the ledger after successful send.

        sent_at: override for backdated entries (external touches logged
        after the fact); defaults to now."""
        entry = ContactLedger(
            project_id=project_id,
            user_id=user_id,
            channel=channel,
            source=source,
            source_id=str(source_id) if source_id else None,
            sent_at=sent_at or self.clock.utcnow(),
        )
        self.db.add(entry)
        self.db.flush()

    def record_contact_once(
        self,
        project_id: int,
        user_id: int,
        channel: str,
        source: str,
        source_id: int,
        sent_at: Optional[datetime] = None,
    ) -> bool:
        """Idempotently ledger a durable send identified by source/source_id."""
        if self.db.get_bind().dialect.name == "postgresql":
            from sqlalchemy import text
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"contact-ledger:{project_id}:{source}:{source_id}"},
            )
        exists = self.db.query(ContactLedger.id).filter(
            ContactLedger.project_id == project_id,
            ContactLedger.user_id == user_id,
            ContactLedger.channel == channel,
            ContactLedger.source == source,
            ContactLedger.source_id == str(source_id),
        ).first()
        if exists:
            return False
        self.record_contact(
            project_id=project_id,
            user_id=user_id,
            channel=channel,
            source=source,
            source_id=source_id,
            sent_at=sent_at,
        )
        return True

    def get_project_policy(self, project_id: int) -> Optional[ProjectPolicy]:
        """Get the project policy config (public method)."""
        return self._get_project_policy(project_id)

    def upsert_project_policy(self, project_id: int, config: Dict[str, Any]) -> ProjectPolicy:
        """Create or update the project policy."""
        existing = self._get_project_policy(project_id)

        if existing:
            for key in ("contact_caps", "channel_cooldowns", "quiet_hours",
                        "suppression_config", "priority_rules", "is_active"):
                if key in config:
                    setattr(existing, key, config[key])
            existing.updated_at = self.clock.utcnow()
            self.db.commit()
            return existing

        policy = ProjectPolicy(
            project_id=project_id,
            contact_caps=config.get("contact_caps"),
            channel_cooldowns=config.get("channel_cooldowns"),
            quiet_hours=config.get("quiet_hours"),
            suppression_config=config.get("suppression_config"),
            priority_rules=config.get("priority_rules"),
            is_active=config.get("is_active", True),
        )
        self.db.add(policy)
        self.db.commit()
        return policy

    def get_contact_stats(self, project_id: int) -> Dict[str, Any]:
        """Get contact stats for a project: counts per channel for today/week/month."""
        now = self.clock.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)

        from app.models import ChannelCapability
        channels = [c.channel for c in self.db.query(ChannelCapability.channel).all()]
        result = {"channels": [], "total_today": 0, "total_this_week": 0, "total_this_month": 0}

        for ch in channels:
            today_count = self.db.query(func.count(ContactLedger.id)).filter(
                ContactLedger.project_id == project_id,
                ContactLedger.channel == ch,
                ContactLedger.sent_at >= today_start,
            ).scalar() or 0

            week_count = self.db.query(func.count(ContactLedger.id)).filter(
                ContactLedger.project_id == project_id,
                ContactLedger.channel == ch,
                ContactLedger.sent_at >= week_start,
            ).scalar() or 0

            month_count = self.db.query(func.count(ContactLedger.id)).filter(
                ContactLedger.project_id == project_id,
                ContactLedger.channel == ch,
                ContactLedger.sent_at >= month_start,
            ).scalar() or 0

            result["channels"].append({
                "channel": ch,
                "today": today_count,
                "this_week": week_count,
                "this_month": month_count,
            })
            result["total_today"] += today_count
            result["total_this_week"] += week_count
            result["total_this_month"] += month_count

        return result

    def get_ledger(
        self,
        project_id: int,
        user_id: Optional[int] = None,
        channel: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ContactLedger]:
        """Get contact ledger entries with optional filters."""
        query = self.db.query(ContactLedger).filter(
            ContactLedger.project_id == project_id,
        )
        if user_id:
            query = query.filter(ContactLedger.user_id == user_id)
        if channel:
            query = query.filter(ContactLedger.channel == channel)

        return query.order_by(ContactLedger.sent_at.desc()).offset(offset).limit(limit).all()

    # ------------------------------------------------------------------
    # Internal checks
    # ------------------------------------------------------------------

    def _get_project_policy(self, project_id: int) -> Optional[ProjectPolicy]:
        return self.db.query(ProjectPolicy).filter(
            ProjectPolicy.project_id == project_id,
        ).first()

    def _locale_quiet_override(self, project_id: int, user_id: int):
        """Return (override_quiet_hours|None, contact_timezone|None) for this contact's
        locale. No-op (None, None) when USE_LOCALE_RESOLUTION is off."""
        from app.services.engine_rollout_service import effective_mode
        if effective_mode(self.db, project_id, "locale_resolution") != "enforce":
            return None, None
        try:
            from app.models.messaging import MessagingUser
            from app.models import LocalePolicyOverride
            contact = self.db.query(MessagingUser).filter(MessagingUser.id == user_id).first()
            if not contact or not contact.locale:
                return None, getattr(contact, "timezone", None) if contact else None
            ov = self.db.query(LocalePolicyOverride).filter(
                LocalePolicyOverride.project_id == project_id,
                LocalePolicyOverride.locale == contact.locale,
            ).first()
            override_quiet = ov.quiet_hours if ov else None
            return override_quiet, (ov.timezone if (ov and ov.timezone) else contact.timezone)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("locale quiet override lookup failed: %s", e)
            return None, None

    def _check_quiet_hours(
        self,
        policy: ProjectPolicy,
        channel: str,
        override_quiet: Optional[dict] = None,
        contact_tz: Optional[str] = None,
    ) -> PolicyDecision:
        """Check if current time falls within quiet hours.
        A per-locale override (override_quiet) takes precedence over the project policy,
        and contact_tz (the contact's timezone) overrides the configured timezone."""
        quiet = override_quiet or policy.quiet_hours
        if not quiet or not quiet.get("enabled"):
            return PolicyDecision(allowed=True)

        # Check if channel is affected
        affected_channels = quiet.get("channels", [])
        if affected_channels and channel not in affected_channels:
            return PolicyDecision(allowed=True)

        tz_name = contact_tz or quiet.get("timezone", "UTC")
        start_str = quiet.get("start", "22:00")
        end_str = quiet.get("end", "08:00")

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
            now_local = self.clock.now(tz)
            current_time = now_local.strftime("%H:%M")

            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))

            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            now_minutes = now_local.hour * 60 + now_local.minute

            if start_minutes > end_minutes:
                # Spans midnight (e.g., 22:00 - 08:00)
                in_quiet = now_minutes >= start_minutes or now_minutes < end_minutes
            else:
                in_quiet = start_minutes <= now_minutes < end_minutes

            if in_quiet:
                # Compute defer_until = next quiet hours end in UTC
                try:
                    end_local = now_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
                    if end_local <= now_local:
                        end_local += timedelta(days=1)
                    defer_until_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
                except Exception:
                    defer_until_utc = None

                return PolicyDecision(
                    allowed=False,
                    reason=f"Quiet hours active ({start_str}-{end_str} {tz_name})",
                    policy_violated="quiet_hours",
                    deferrable=True,
                    defer_until=defer_until_utc,
                )
        except Exception as e:
            logger.warning(f"Error checking quiet hours: {e}")

        return PolicyDecision(allowed=True)

    def _check_channel_cooldown(
        self, policy: ProjectPolicy, project_id: int, user_id: int, channel: str
    ) -> PolicyDecision:
        """Check if enough time has passed since last contact on this channel."""
        cooldowns = policy.channel_cooldowns
        if not cooldowns:
            return PolicyDecision(allowed=True)

        cooldown_seconds = cooldowns.get(channel)
        if not cooldown_seconds:
            return PolicyDecision(allowed=True)

        cutoff = self.clock.utcnow() - timedelta(seconds=cooldown_seconds)
        recent = self.db.query(ContactLedger).filter(
            ContactLedger.project_id == project_id,
            ContactLedger.user_id == user_id,
            ContactLedger.channel == channel,
            ContactLedger.sent_at >= cutoff,
        ).first()

        if recent:
            defer_until = recent.sent_at + timedelta(seconds=cooldown_seconds)
            return PolicyDecision(
                allowed=False,
                reason=f"Channel cooldown: {channel} (min {cooldown_seconds}s between messages)",
                policy_violated="channel_cooldown",
                deferrable=True,
                defer_until=defer_until,
            )

        return PolicyDecision(allowed=True)

    def _check_contact_caps(
        self, policy: ProjectPolicy, project_id: int, user_id: int, channel: str
    ) -> PolicyDecision:
        """Check daily/weekly/monthly contact caps."""
        caps = policy.contact_caps
        if not caps:
            return PolicyDecision(allowed=True)

        now = self.clock.utcnow()
        daily_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        weekly_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        monthly_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (
            monthly_start.replace(year=monthly_start.year + 1, month=1)
            if monthly_start.month == 12
            else monthly_start.replace(month=monthly_start.month + 1)
        )
        periods = {
            "daily": (daily_start, daily_start + timedelta(days=1)),
            "weekly": (weekly_start, weekly_start + timedelta(days=7)),
            "monthly": (monthly_start, next_month),
        }

        for period_name, (period_start, next_period_start) in periods.items():
            period_caps = caps.get(period_name)
            if not period_caps:
                continue

            # Check channel-specific cap
            channel_cap = period_caps.get(channel)
            if channel_cap is not None:
                count = self.db.query(func.count(ContactLedger.id)).filter(
                    ContactLedger.project_id == project_id,
                    ContactLedger.user_id == user_id,
                    ContactLedger.channel == channel,
                    ContactLedger.sent_at >= period_start,
                ).scalar() or 0

                if count >= channel_cap:
                    return PolicyDecision(
                        allowed=False,
                        reason=f"{period_name} {channel} cap exceeded ({count}/{channel_cap})",
                        policy_violated=f"{period_name}_cap",
                        deferrable=True,
                        defer_until=next_period_start,
                    )

            # Check total cap
            total_cap = period_caps.get("total")
            if total_cap is not None:
                total_count = self.db.query(func.count(ContactLedger.id)).filter(
                    ContactLedger.project_id == project_id,
                    ContactLedger.user_id == user_id,
                    ContactLedger.sent_at >= period_start,
                ).scalar() or 0

                if total_count >= total_cap:
                    return PolicyDecision(
                        allowed=False,
                        reason=f"{period_name} total cap exceeded ({total_count}/{total_cap})",
                        policy_violated=f"{period_name}_total_cap",
                        deferrable=True,
                        defer_until=next_period_start,
                    )

        return PolicyDecision(allowed=True)

    def _check_priority(
        self, policy: ProjectPolicy, project_id: int, user_id: int, source: str
    ) -> PolicyDecision:
        """Check if a higher-priority source contacted this user recently."""
        rules = policy.priority_rules
        if not rules:
            return PolicyDecision(allowed=True)

        order = rules.get("order", [])
        window_seconds = rules.get("conflict_window_seconds", 60)

        if not order or source not in order:
            return PolicyDecision(allowed=True)

        source_idx = order.index(source)
        if source_idx == 0:
            return PolicyDecision(allowed=True)  # Highest priority

        # Check if any higher-priority source contacted recently
        higher_sources = order[:source_idx]
        cutoff = self.clock.utcnow() - timedelta(seconds=window_seconds)

        recent_higher = self.db.query(ContactLedger).filter(
            ContactLedger.project_id == project_id,
            ContactLedger.user_id == user_id,
            ContactLedger.source.in_(higher_sources),
            ContactLedger.sent_at >= cutoff,
        ).first()

        if recent_higher:
            return PolicyDecision(
                allowed=False,
                reason=f"Higher-priority source '{recent_higher.source}' contacted user recently",
                policy_violated="priority_conflict",
            )

        return PolicyDecision(allowed=True)
