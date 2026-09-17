"""Unified outbound boundary for source contracts, attention and delivery.

The implementation has more than the original seven transport steps: it first
resolves the registered source/lane, protects synthetic recipients, handles
Candidate/Selection and Guardian policy, then performs channel resolution,
opt-out/verification checks, content negotiation and provider delivery.
"""
import logging
import hashlib
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.models import (
    SendLog, ProjectSendConfig, ContactRateWindow,
)
from app.models.messaging import MessagingEvent
from app.services.channels.base import (
    OutboundContent, SendDecision,
)
from app.services.channels.registry import ChannelRegistry
from app.services.channels.send_pace_service import SendPaceService, pace_mode

logger = logging.getLogger(__name__)


class SendService:
    """Orchestrates the shared source, attention and provider send pipeline."""

    def __init__(self, db: Session):
        self.db = db

    async def send(
        self,
        project_id: int,
        user_id: Optional[int],
        recipient: str,
        content: OutboundContent,
        channel: Optional[str] = None,
        fallback_order: Optional[List[str]] = None,
        on_channel_unavailable: Optional[str] = None,
        source_type: str = "manual",
        source_id: Optional[int] = None,
        instance_config: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[datetime] = None,
        skip_policy: bool = False,
        # Named policy profiles let high-risk callers opt into a stricter,
        # stable contract without depending on project-wide rollout flags.
        # Campaigns always use the strict promotional profile.
        policy_profile: Optional[str] = None,
        policy_context: Optional[Dict[str, Any]] = None,
        template_id: Optional[int] = None,
        # Pass-through fields for retry/deferred context propagation
        render_context: Optional[Dict[str, Any]] = None,
        deferred_source_enrollment_id: Optional[int] = None,
        retry_of_id: Optional[int] = None,
        # Dispatch of an already-parked row (deferred/delayed): finalize THAT
        # row in place instead of creating a second SendLog. When set, the
        # quiet-hours delay path re-defers this same row rather than minting an
        # orphan. This is what keeps a deferred delivery to one SendLog row.
        existing_send_log_id: Optional[int] = None,
        # Shadow/parity mode: run the full decision pipeline (channel
        # resolution + all gates) but DELIVER NOTHING and write NO SendLog/
        # ledger. Returns the decision that WOULD result (status "would_send"
        # with channel_used, or the blocking status). Used to parity-check a
        # bypass path against send() before cutting it over (0D).
        dry_run: bool = False,
        # Authored slot identity — stored on the SendLog so the bandit can
        # attribute the outcome to a stable arm (Phase 4).
        slot_id: Optional[str] = None,
    ) -> SendDecision:
        """Execute the full send pipeline. Returns a SendDecision."""
        trace: List[Dict[str, Any]] = []
        _campaign_ignored_rules: set[str] = set()
        if policy_profile == "campaign":
            # A campaign route is sticky and never falls back to a different
            # sender identity implicitly. Permission was evaluated by the run
            # worker and must be asserted for this exact dispatch.
            source_type = "campaign"
            skip_policy = False
            fallback_order = [channel] if channel else []
            on_channel_unavailable = "fail"
            policy_context = policy_context or {}
            trace.append({
                "step": "policy_profile",
                "profile": "campaign",
                "permission_policy": policy_context.get("permission_policy"),
                "permission_authorized": bool(policy_context.get("permission_authorized")),
                "ts": datetime.utcnow().isoformat(),
            })

        # Context fields propagated to every log created in this pipeline
        _ctx = {
            "render_context": render_context,
            "deferred_source_enrollment_id": deferred_source_enrollment_id,
            "retry_of_id": retry_of_id,
            "existing_send_log_id": existing_send_log_id,
            "dry_run": dry_run,
            "slot_id": slot_id,
            # Resolved sending domain (email) — stamped on every SendLog so
            # bounces map back to a domain and the pace governor has its key.
            "sending_domain": None,
        }
        try:
            _ctx["sending_domain"] = SendPaceService(self.db).resolve_sending_domain(
                channel, project_id, instance_config,
            )
        except Exception:
            pass

        # ── Source registration / shared-attention contract ─────────
        # This is the fail-closed platform boundary. A new source cannot gain
        # provider access merely by inventing a source_type string, and a
        # promotional automation cannot run while Candidate/Selection or its
        # authored purpose/attention identity are missing.
        from app.services.channels.source_contract import resolve_dispatch_source
        try:
            _source_resolution = resolve_dispatch_source(
                self.db, project_id, source_type, source_id,
            )
            _lane = _source_resolution.lane
            trace.append({
                "step": "source_contract",
                "result": "accepted",
                "source_type": source_type,
                "attention_participation": (
                    _source_resolution.contract.attention_participation.value
                ),
                "activation_kind": _source_resolution.contract.activation_kind,
                "ts": datetime.utcnow().isoformat(),
            })
        except Exception as exc:
            error = f"Outbound source contract blocked dispatch: {exc}"
            trace.append({
                "step": "source_contract",
                "result": "blocked",
                "source_type": source_type,
                "reason": str(exc),
                "ts": datetime.utcnow().isoformat(),
            })
            logger.error("[source-contract] %s", error)
            return self._create_failed_log(
                project_id, user_id, recipient, content, channel,
                fallback_order, source_type, source_id, template_id, trace,
                error=error, status="blocked", **_ctx,
            )

        # ── Guard: never deliver to a synthetic placeholder address ──
        # Some tenants represent anonymous contacts with a placeholder email
        # (e.g. guest_<uuid>@guest.tabloide.pro) — not a real mailbox; sending
        # hard-bounces and hurts sender reputation. The pattern is per-project
        # (ProjectSendConfig.placeholder_email_pattern), so this stays tenant-
        # agnostic. Central catch-all so no upstream path can leak one.
        from app.services.messaging.recipient_validation import (
            is_placeholder_email, project_placeholder_pattern,
        )
        if is_placeholder_email(recipient, project_placeholder_pattern(self.db, project_id)):
            trace.append({
                "step": "recipient_guard",
                "result": "blocked_placeholder",
                "ts": datetime.utcnow().isoformat(),
            })
            return self._create_failed_log(
                project_id, user_id, recipient, content, channel,
                fallback_order, source_type, source_id, template_id, trace,
                error="Placeholder recipient blocked", status="skipped", **_ctx,
            )

        # ── Lane classification / attention participation ────────────
        # The resolved lane is both audited and used by the pause,
        # Candidate/Selection, consent and Guardian phases below.
        from app.services.channels.lanes import Lane
        _lane_known = True
        trace.append({
            "step": "lane",
            "lane": _lane.value,
            "source_type": source_type,
            "known": _lane_known,
            "ts": datetime.utcnow().isoformat(),
        })

        # ── Automation pause gate ────────────────────────────────────
        # A paused contact receives no automated messages while an operator
        # handles them manually. MANUAL and TRANSACTIONAL lanes pass (manual
        # sends must keep working; a delivery retry belongs to a pre-pause
        # message). CONVERSATIONAL is always skipped — a chat reply delivered
        # after resume is worse than none. PROMOTIONAL honors the pause mode:
        # 'hold' parks as a delayed row the worker excludes while paused (so
        # it dispatches on resume), 'skip' drops it. Placed BEFORE candidate
        # emission so pause wins regardless of the candidate flag. Worker
        # re-dispatches (existing_send_log_id) re-park that same row in
        # place — race defense only; the next worker cycle excludes it.
        if user_id and not dry_run and _lane not in (Lane.MANUAL, Lane.TRANSACTIONAL):
            from app.services.messaging.contact_pause_service import is_contact_paused
            _paused_user = is_contact_paused(self.db, project_id, user_id)
            if _paused_user:
                _pause_mode = _paused_user.automations_pause_mode or "hold"
                _pause_result = (
                    "held" if _lane == Lane.PROMOTIONAL and _pause_mode == "hold"
                    else "skipped"
                )
                trace.append({
                    "step": "automations_paused",
                    "mode": _pause_mode,
                    "lane": _lane.value,
                    "result": _pause_result,
                    "ts": datetime.utcnow().isoformat(),
                })
                if _pause_result == "held":
                    return self._create_scheduled_log(
                        project_id, user_id, recipient, content, channel,
                        fallback_order, source_type, source_id, template_id,
                        trace, scheduled_at=scheduled_at or datetime.utcnow(), **_ctx,
                    )
                return self._create_failed_log(
                    project_id, user_id, recipient, content, channel,
                    fallback_order, source_type, source_id, template_id, trace,
                    error="Contact automations paused", status="skipped", **_ctx,
                )

        # ── Phase 2: candidate emission ──────────────────────────────
        # A PROMOTIONAL immediate send parks as a durable status='candidate'
        # row to contest the contact's next slot instead of firing now. Only
        # for fresh sends — never a worker dispatch (existing_send_log_id set),
        # a dry_run, or a future-scheduled send. The selection sweep dispatches
        # the per-contact winner; unselected candidates expire after the TTL.
        from app.services.channels.selection import candidate_mode
        if (candidate_mode(self.db, project_id) == "enforce" and _lane == Lane.PROMOTIONAL
                and existing_send_log_id is None and not dry_run
                and not (scheduled_at and scheduled_at > datetime.utcnow())):
            return self._park_as_candidate(
                project_id, user_id, recipient, content, channel,
                fallback_order, source_type, source_id, template_id, trace, _ctx,
            )

        # Load project send config
        send_config = self._get_send_config(project_id)
        strategy = on_channel_unavailable or (send_config.default_strategy if send_config else "try_fallback")

        # ── Step 1: Resolve channel order ────────────────────────────
        channels = self._resolve_channels(channel, fallback_order, send_config, project_id)
        trace.append({
            "step": "resolve_channels",
            "result": "ok",
            "channels": channels,
            "ts": datetime.utcnow().isoformat(),
        })

        if not channels:
            return self._create_failed_log(
                project_id, user_id, recipient, content, channel, fallback_order,
                source_type, source_id, template_id, trace,
                error="No channels available", **_ctx,
            )

        # Resolve the persisted campaign exception before the single Guardian
        # gate. The row, not caller-provided context, is authoritative.
        _campaign_override_id = None
        if policy_profile == "campaign" and user_id and source_id is not None:
            from app.models.campaigns import CampaignPolicyOverride, CampaignRecipient
            from app.services.campaigns.policy_overrides import active_override_rules

            override = self.db.query(CampaignPolicyOverride).join(
                CampaignRecipient,
                (CampaignRecipient.project_id == CampaignPolicyOverride.project_id)
                & (CampaignRecipient.run_id == CampaignPolicyOverride.run_id),
            ).filter(
                CampaignRecipient.id == source_id,
                CampaignRecipient.project_id == project_id,
                CampaignRecipient.user_id == user_id,
            ).first()
            ignored_rules = active_override_rules(override)
            _campaign_ignored_rules = ignored_rules
            _campaign_override_id = override.id if ignored_rules and override else None

        # ── Step 1.5: single Guardian gate (attention / pacing) ─────
        # Lane-aware: transactional/conversational pass, manual warns, promo
        # gated (delay/drop). Flag SEND_GUARDIAN_MODE (off|shadow|enforce);
        # closes the gap that send() never enforced PolicyService caps. Not a
        # Worker dispatches are deliberately re-evaluated here so a Selection
        # winner and every deferred send cross Guardian immediately before I/O.
        from app.services.channels.consolidation import guardian_mode
        _g_mode = "enforce" if policy_profile == "campaign" else guardian_mode(self.db, project_id)
        if _g_mode != "off" and user_id and channels and not dry_run:
            from app.services.guardian.guardian_service import GuardianService
            _verdict = GuardianService(self.db).assess(
                project_id, user_id, channels[0], _lane.value, source_type, source_id,
                ignored_policies=_campaign_ignored_rules,
            )
            if _verdict.warnings:
                logger.info("[guardian] %s warnings: %s", _lane.value, _verdict.warnings)
            if _verdict.verb != "send":
                trace.append({
                    "step": "guardian", "verb": _verdict.verb,
                    "reason": _verdict.reason, "mode": _g_mode,
                    "policy_override_id": _campaign_override_id,
                    "overridden_rules": sorted(_campaign_ignored_rules),
                    "ts": datetime.utcnow().isoformat(),
                })
                if _g_mode == "enforce":
                    if _verdict.verb == "delay" and _verdict.defer_until:
                        if self._policy_window_expired(
                            _verdict.defer_until,
                            policy_context,
                            existing_send_log_id,
                        ):
                            return self._create_failed_log(
                                project_id, user_id, recipient, content, channel,
                                fallback_order, source_type, source_id, template_id,
                                trace,
                                error="Policy defer exceeds delivery deadline or TTL",
                                status="policy_window_expired",
                                **_ctx,
                            )
                        return self._create_scheduled_log(
                            project_id, user_id, recipient, content, channel,
                            fallback_order, source_type, source_id, template_id,
                            trace, scheduled_at=_verdict.defer_until, **_ctx,
                        )
                    return self._create_failed_log(
                        project_id, user_id, recipient, content, channel,
                        fallback_order, source_type, source_id, template_id, trace,
                        error=f"Guardian: {_verdict.reason}", status="skipped", **_ctx,
                    )
                logger.info(
                    "[guardian][shadow] would %s (%s): %s",
                    _verdict.verb, _lane.value, _verdict.reason,
                )

        # ── Step 2: Opt-out check ────────────────────────────────────
        channels = self._filter_opt_out(project_id, user_id, channels, trace)
        if not channels:
            return self._create_failed_log(
                project_id, user_id, recipient, content, channel, fallback_order,
                source_type, source_id, template_id, trace,
                error="All channels blocked by opt-out",
                status="blocked", **_ctx,
            )

        # ── Step 2.25: Deliverability verification policy ──────────
        # This is independent from consent/subscription/opt-out. It only
        # suppresses fresh, conclusive results for the identifier currently
        # stored on the contact.
        channels = self._filter_verification_policy(
            project_id, user_id, channels, trace, policy_profile=policy_profile,
            policy_context=policy_context,
        )
        if not channels:
            return self._create_failed_log(
                project_id, user_id, recipient, content, channel, fallback_order,
                source_type, source_id, template_id, trace,
                error="All channels blocked by contact verification policy",
                status="blocked", **_ctx,
            )

        # ── Step 2.5: Consent gate (0B — Promotional lane, SHADOW) ────
        # Marketing consent applies only to the Promotional lane. SHADOW by
        # default: logs would-suppress, filters nothing. Flip per channel via
        # SEND_CONSENT_ENFORCE only AFTER grandfathering consent_channels —
        # enforcing cold suppresses nearly all WA/SMS promo traffic (hazard H2).
        if policy_profile == "campaign" and user_id:
            if not policy_context.get("permission_authorized"):
                trace.append({
                    "step": "campaign_permission",
                    "result": "blocked",
                    "ts": datetime.utcnow().isoformat(),
                })
                return self._create_failed_log(
                    project_id, user_id, recipient, content, channel, fallback_order,
                    source_type, source_id, template_id, trace,
                    error="Campaign permission was not authorized at dispatch",
                    status="skipped", **_ctx,
                )
            trace.append({
                "step": "campaign_permission",
                "result": "authorized",
                "policy": policy_context.get("permission_policy"),
                "evidence_id": policy_context.get("permission_evidence_id"),
                "ts": datetime.utcnow().isoformat(),
            })
        elif _lane == Lane.PROMOTIONAL and user_id:
            channels = self._consent_filter(project_id, user_id, channels, trace)
            if not channels:
                return self._create_failed_log(
                    project_id, user_id, recipient, content, channel, fallback_order,
                    source_type, source_id, template_id, trace,
                    error="All channels blocked by missing consent",
                    status="blocked", **_ctx,
                )

        # ── Step 4: Quiet hours check ────────────────────────────────
        if _g_mode == "enforce":
            trace.append({
                "step": "quiet_hours",
                "result": "handled_by_guardian",
                "ts": datetime.utcnow().isoformat(),
            })
        elif (
            send_config
            and send_config.quiet_hours_enabled
            and "quiet_hours" in _campaign_ignored_rules
        ):
            trace.append({
                "step": "quiet_hours",
                "result": "overridden",
                "scope": "campaign_run",
                "ts": datetime.utcnow().isoformat(),
            })
        elif send_config and send_config.quiet_hours_enabled:
            quiet_active = self._is_quiet_hours(send_config)
            if quiet_active:
                trace.append({
                    "step": "quiet_hours",
                    "result": "active",
                    "action": send_config.quiet_hours_action,
                    "ts": datetime.utcnow().isoformat(),
                })
                if send_config.quiet_hours_action == "skip":
                    return self._create_failed_log(
                        project_id, user_id, recipient, content, channel,
                        fallback_order, source_type, source_id, template_id, trace,
                        error="Skipped due to quiet hours", status="skipped", **_ctx,
                    )
                elif send_config.quiet_hours_action == "delay":
                    # Schedule for quiet hours end
                    resume_at = self._quiet_hours_resume_time(send_config)
                    return self._create_scheduled_log(
                        project_id, user_id, recipient, content, channel,
                        fallback_order, source_type, source_id, template_id,
                        trace, scheduled_at=resume_at, **_ctx,
                    )
            else:
                trace.append({
                    "step": "quiet_hours",
                    "result": "inactive",
                    "ts": datetime.utcnow().isoformat(),
                })

        # ── Scheduled send ───────────────────────────────────────────
        if scheduled_at and scheduled_at > datetime.utcnow():
            trace.append({
                "step": "scheduled",
                "result": "deferred",
                "scheduled_at": scheduled_at.isoformat(),
                "ts": datetime.utcnow().isoformat(),
            })
            return self._create_scheduled_log(
                project_id, user_id, recipient, content, channel,
                fallback_order, source_type, source_id, template_id,
                trace, scheduled_at=scheduled_at, **_ctx,
            )

        # ── Step 4.75: Best-time-to-send window (PROMOTIONAL only) ───
        # Contact-timezone-aware send windows with inheritance:
        # contact+channel > contact > project channel > project default.
        # Outside the window → park as delayed until the next window start;
        # the worker re-runs send() at dispatch, so the window is re-checked
        # (and re-deferred if the dispatch slipped past the window). Placed
        # AFTER the explicit-schedule park so future-scheduled sends are
        # window-checked at their actual dispatch time, not at enqueue.
        if _lane == Lane.PROMOTIONAL and user_id:
            from app.services.channels.send_windows import (
                resolve_window, resolve_timezone, is_within_window, next_window_start,
            )
            from app.models.messaging import MessagingUser as _MU
            _bts_user = self.db.query(_MU).filter(_MU.id == user_id).first()
            _user_windows = getattr(_bts_user, "send_windows", None) if _bts_user else None
            _project_windows = getattr(send_config, "send_windows", None) if send_config else None
            if _user_windows or _project_windows:
                _bts_channel = channels[0] if channels else channel
                _win_cfg, _win_source = resolve_window(_user_windows, _project_windows, _bts_channel)
                if _win_cfg is None:
                    trace.append({
                        "step": "send_window", "result": "none",
                        "window_source": _win_source,
                        "ts": datetime.utcnow().isoformat(),
                    })
                else:
                    _project_tz = None
                    if _bts_user and _bts_user.project_id:
                        from app.models import Project as _Proj
                        _proj = self.db.query(_Proj.default_timezone).filter(
                            _Proj.id == project_id,
                        ).first()
                        _project_tz = _proj[0] if _proj else None
                    _tz = resolve_timezone(
                        getattr(_bts_user, "timezone", None), _project_tz,
                    )
                    _now = datetime.utcnow()
                    if is_within_window(_win_cfg, _now, _tz):
                        trace.append({
                            "step": "send_window", "result": "ok",
                            "window_source": _win_source, "tz": str(_tz),
                            "ts": _now.isoformat(),
                        })
                    else:
                        _resume_at = next_window_start(_win_cfg, _now, _tz)
                        if _resume_at is None:
                            trace.append({
                                "step": "send_window", "result": "ok",
                                "warning": "no_window_found",
                                "window_source": _win_source, "tz": str(_tz),
                                "ts": _now.isoformat(),
                            })
                        else:
                            trace.append({
                                "step": "send_window", "result": "delayed",
                                "window_source": _win_source, "tz": str(_tz),
                                "resume_at": _resume_at.isoformat(),
                                "ts": _now.isoformat(),
                            })
                            return self._create_scheduled_log(
                                project_id, user_id, recipient, content, channel,
                                fallback_order, source_type, source_id, template_id,
                                trace, scheduled_at=_resume_at, **_ctx,
                            )

        # ── Step 4.5: Outbound pace governor ─────────────────────────
        # Smooth per-sending-domain throughput so a burst can't get the domain
        # throttled by receivers. Over-budget sends park as a delayed SendLog
        # (drained by scheduled_send_worker). Skipped for dry_run and for worker
        # re-dispatch of an already-parked row (it reserved its slot at enqueue).
        _pace_mode = pace_mode(self.db, project_id)
        if _pace_mode != "off" and not dry_run and existing_send_log_id is None and channels:
            pace_domain = _ctx.get("sending_domain")
            if pace_domain is None:
                try:
                    pace_domain = SendPaceService(self.db).resolve_sending_domain(
                        channels[0], project_id, instance_config,
                    )
                    _ctx["sending_domain"] = pace_domain
                except Exception:
                    pace_domain = None
            if pace_domain:
                _priority = self._lane_priority(_lane)
                reserve = SendPaceService(self.db).reserve_slot(
                    pace_domain, project_id, priority=_priority,
                    apply=(_pace_mode == "enforce"),
                )
                trace.append({
                    "step": "pace",
                    "mode": _pace_mode,
                    "domain": pace_domain,
                    "decision": reserve.decision,
                    "reason": reserve.reason,
                    "scheduled_at": reserve.scheduled_at.isoformat() if reserve.scheduled_at else None,
                    "ts": datetime.utcnow().isoformat(),
                })
                if _pace_mode == "enforce" and reserve.decision in ("delay", "hold") and reserve.scheduled_at:
                    return self._create_scheduled_log(
                        project_id, user_id, recipient, content, channels[0],
                        fallback_order, source_type, source_id, template_id,
                        trace, scheduled_at=reserve.scheduled_at, priority=_priority, **_ctx,
                    )

        # ── Iterate channels (steps 3, 5, 6, 7) ─────────────────────
        for attempt_idx, ch in enumerate(channels):
            adapter = ChannelRegistry.get_adapter(ch)
            if not adapter:
                trace.append({
                    "step": "adapter_lookup",
                    "channel": ch,
                    "result": "not_found",
                    "ts": datetime.utcnow().isoformat(),
                })
                continue

            # Step 3: Session window / constraint check
            constraints = adapter.check_constraints(
                self.db, project_id, recipient, instance_config,
            )
            if not constraints.available:
                trace.append({
                    "step": "constraints",
                    "channel": ch,
                    "result": "unavailable",
                    "reason": constraints.reason,
                    "ts": datetime.utcnow().isoformat(),
                })
                if strategy == "fail":
                    break
                continue

            # Skip channel only if window is closed AND no template fallback.
            # The adapter handles window-gated sends agnostically: if content has
            # template_name, it falls back to a template send when the window is closed.
            # Messenger/Instagram human replies may bypass via the HUMAN_AGENT tag
            # (content.metadata["human_agent"]) — the adapter applies the tag.
            if (
                constraints.session_window_open is False
                and content.content_type != "template"
                and not content.template_name
                and not content.metadata.get("human_agent")
            ):
                trace.append({
                    "step": "session_window",
                    "channel": ch,
                    "result": "closed",
                    "ts": datetime.utcnow().isoformat(),
                })
                if strategy == "fail":
                    break
                continue

            # Step 5: Rate limit check
            rl_messages = (send_config.rate_limit_messages if send_config else None) or 10
            rl_window = (send_config.rate_limit_window_minutes if send_config else None) or 5
            if self._check_rate_limit(
                project_id, recipient, ch,
                rl_messages,
                rl_window,
            ):
                trace.append({
                    "step": "rate_limit",
                    "channel": ch,
                    "result": "limited",
                    "ts": datetime.utcnow().isoformat(),
                })
                if strategy == "fail":
                    break
                continue

            trace.append({
                "step": "rate_limit",
                "channel": ch,
                "result": "ok",
                "ts": datetime.utcnow().isoformat(),
            })

            # Dry-run parity: this channel passed every gate and would be the
            # one delivered on. Return WITHOUT negotiating/delivering/logging.
            if dry_run:
                return SendDecision(
                    success=True,
                    status="would_send",
                    channel_used=ch,
                    decision_trace=trace,
                )

            # Step 6: Content negotiation
            capabilities = ChannelRegistry.get_capabilities(self.db, ch)
            negotiated = adapter.negotiate_content(content, capabilities)

            trace.append({
                "step": "content_negotiation",
                "channel": ch,
                "result": "ok",
                "ts": datetime.utcnow().isoformat(),
            })

            duplicate = self._find_recent_whatsapp_duplicate(
                project_id=project_id,
                user_id=user_id,
                recipient=recipient,
                content=negotiated,
                channel=ch,
                source_type=source_type,
                template_id=template_id,
                existing_send_log_id=existing_send_log_id,
                dry_run=dry_run,
            )
            if duplicate:
                trace.append({
                    "step": "duplicate_guard",
                    "channel": ch,
                    "result": "skipped",
                    "duplicate_send_log_id": duplicate.id,
                    "ts": datetime.utcnow().isoformat(),
                })
                return self._create_failed_log(
                    project_id, user_id, recipient, negotiated, ch, fallback_order,
                    source_type, source_id, template_id, trace,
                    error=f"Duplicate WhatsApp send suppressed; existing send_log={duplicate.id}",
                    status="skipped", **_ctx,
                )

            # Step 6.5: Inject email tracking pixel + rewrite links for click tracking.
            # utm_content carries the tracking token so post-click page/track
            # events can be hard-linked back to this SendLog (MES attribution).
            tracking_token = None
            if ch == "email" and negotiated.html:
                from app.services.email_tracking_service import EmailTrackingService
                import os
                tracking_token = EmailTrackingService.generate_token()
                api_base = os.getenv("API_BASE_URL", "").rstrip("/")
                tracking_base = os.getenv("TRACKING_BASE_URL", api_base).rstrip("/")
                # Inject open pixel
                tracking_url = f"{api_base}/t/o/{tracking_token}.gif"
                negotiated.html = EmailTrackingService.inject_pixel(negotiated.html, tracking_url)
                # Rewrite links for click tracking
                if tracking_base:
                    negotiated.html = EmailTrackingService.rewrite_links(
                        negotiated.html, tracking_base, tracking_token,
                        utm_params={
                            "utm_source": "versya",
                            "utm_medium": ch,
                            "utm_content": tracking_token,
                        },
                    )
            elif ch in ("whatsapp", "sms") and getattr(negotiated, "text", None):
                # Plain-text link rewriting is opt-in per project
                # (mes_config.link_tracking_channels) — rewritten domains can
                # look unfamiliar to recipients on chat channels.
                if ch in self._link_tracking_channels(project_id):
                    from app.services.email_tracking_service import EmailTrackingService
                    import os
                    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
                    tracking_base = os.getenv("TRACKING_BASE_URL", api_base).rstrip("/")
                    if tracking_base and EmailTrackingService._TEXT_URL_RE.search(negotiated.text):
                        tracking_token = EmailTrackingService.generate_token()
                        negotiated.text = EmailTrackingService.rewrite_text_links(
                            negotiated.text, tracking_base, tracking_token,
                            utm_params={
                                "utm_source": "versya",
                                "utm_medium": ch,
                                "utm_content": tracking_token,
                            },
                        )

            # Inject user_id into instance_config for unsubscribe header generation
            _cfg = dict(instance_config or {})
            if user_id:
                _cfg["user_id"] = user_id
            if policy_profile:
                _cfg["policy_profile"] = policy_profile
            if policy_context and policy_context.get("locale"):
                _cfg["locale"] = policy_context["locale"]

            if policy_profile == "campaign":
                stable_message_id = str((negotiated.metadata or {}).get("message_id") or "")
                if not stable_message_id:
                    return self._create_failed_log(
                        project_id, user_id, recipient, negotiated, ch, fallback_order,
                        source_type, source_id, template_id, trace,
                        error="Campaign submission requires a deterministic message id",
                        status="failed", **_ctx,
                    )
                # Persist the submission intent (and any pace reservation) in
                # its own transaction before external I/O.  If the process
                # dies during SMTP/API submission, the campaign worker finds
                # `submitting` and fails closed as submission_unknown instead
                # of sending a second email.
                intent_log = self._create_send_log(
                    project_id=project_id,
                    user_id=user_id,
                    channel=ch,
                    recipient=recipient,
                    content=negotiated,
                    preferred_channel=channel,
                    fallback_order=fallback_order,
                    fallback_attempt=attempt_idx,
                    source_type="campaign",
                    source_id=source_id,
                    template_id=template_id,
                    status="submitting",
                    provider_message_id=stable_message_id,
                    decision_trace=trace,
                    tracking_token=tracking_token,
                    instance_id=_cfg.get("instance_id"),
                    **_ctx,
                )
                self.db.commit()
                existing_send_log_id = intent_log.id
                _ctx["existing_send_log_id"] = intent_log.id
                _cfg["send_log_id"] = intent_log.id
                allowed, gate_reason = self._campaign_submission_gate(
                    project_id=project_id,
                    user_id=user_id,
                    source_id=source_id,
                    send_log_id=intent_log.id,
                    channel=ch,
                    policy_context=policy_context or {},
                )
                if not allowed:
                    if gate_reason in {"campaign_contact_paused_hold", "campaign_paused"}:
                        return self._create_scheduled_log(
                            project_id, user_id, recipient, negotiated, ch, fallback_order,
                            source_type, source_id, template_id, trace,
                            scheduled_at=datetime.utcnow() + timedelta(minutes=1),
                            **_ctx,
                        )
                    canceled = gate_reason in {"campaign_run_canceled", "campaign_recipient_canceled"}
                    return self._create_failed_log(
                        project_id, user_id, recipient, negotiated, ch, fallback_order,
                        source_type, source_id, template_id, trace,
                        error=gate_reason,
                        status="canceled" if canceled else "skipped",
                        **_ctx,
                    )

            # Step 7: Deliver
            send_result = await adapter.send(
                self.db, project_id, recipient, negotiated, _cfg,
            )

            if send_result.success:
                trace.append({
                    "step": "submitted_to_provider",
                    "channel": ch,
                    "result": "success",
                    "provider_message_id": send_result.provider_message_id,
                    "ts": datetime.utcnow().isoformat(),
                })
                log = self._create_send_log(
                    project_id=project_id,
                    user_id=user_id,
                    channel=ch,
                    recipient=recipient,
                    content=negotiated,
                    preferred_channel=channel,
                    fallback_order=fallback_order,
                    fallback_attempt=attempt_idx,
                    source_type=source_type,
                    source_id=source_id,
                    template_id=template_id,
                    status="sent",
                    provider_message_id=send_result.provider_message_id,
                    provider_response=send_result.provider_response,
                    decision_trace=trace,
                    sent_at=datetime.utcnow(),
                    tracking_token=tracking_token,
                    instance_id=send_result.instance_id,
                    **_ctx,
                )
                # Emit channel.{ch}.sent event
                self._emit_channel_event(log, "sent")
                # 0D: single contact_ledger writer. Registered manual sends
                # already have no legacy caller-side writer, so they are always
                # ledgered here; the migration flag remains for automated
                # callers that still have compatibility writers elsewhere.
                from app.services.channels.consolidation import ledger_in_send_enabled
                _ledger_at_boundary = (
                    ledger_in_send_enabled(self.db, project_id)
                    or source_type in {"manual", "manual_send"}
                )
                if _ledger_at_boundary and user_id:
                    self._record_ledger_once(
                        project_id=project_id,
                        user_id=user_id,
                        channel=ch,
                        source_type=source_type,
                        source_id=source_id,
                        send_log_id=log.id,
                        sent_at=log.sent_at,
                    )
                elif user_id:
                    from app.services.engine_rollout_service import effective_mode
                    if effective_mode(self.db, project_id, "ledger") == "shadow":
                        log.decision_trace = list(log.decision_trace or []) + [{
                            "step": "ledger_shadow",
                            "result": "would_write",
                            "idempotency_source_id": source_id or log.id,
                            "ts": datetime.utcnow().isoformat(),
                        }]
                return SendDecision(
                    success=True,
                    send_log_id=log.id,
                    channel_used=ch,
                    status="sent",
                    decision_trace=trace,
                    provider_message_id=send_result.provider_message_id,
                )

            # Send failed — record and try next
            trace.append({
                "step": "submitted_to_provider",
                "channel": ch,
                "result": "failed",
                "error": send_result.error,
                "rate_limited": send_result.rate_limited,
                "window_closed": send_result.window_closed,
                "ts": datetime.utcnow().isoformat(),
            })

            # A transport can disappear while SMTP DATA / an API request is
            # in flight.  The provider may already have accepted the email;
            # retrying automatically is a duplicate-send risk.  Persist the
            # ambiguity as a terminal, reconcilable outcome instead.
            if send_result.error_code == "submission_unknown":
                decision = self._create_failed_log(
                    project_id, user_id, recipient, negotiated, ch, fallback_order,
                    source_type, source_id, template_id, trace,
                    error=send_result.error or "Provider submission outcome is unknown",
                    status="submission_unknown",
                    **_ctx,
                )
                from app.services.channels.consolidation import ledger_in_send_enabled
                if ledger_in_send_enabled(self.db, project_id) and user_id:
                    self._record_ledger_once(
                        project_id=project_id,
                        user_id=user_id,
                        channel=ch,
                        source_type=source_type,
                        source_id=source_id,
                        send_log_id=decision.send_log_id,
                        sent_at=datetime.utcnow(),
                    )
                elif user_id:
                    from app.services.engine_rollout_service import effective_mode
                    if effective_mode(self.db, project_id, "ledger") == "shadow":
                        uncertain_log = self.db.query(SendLog).filter(
                            SendLog.id == decision.send_log_id,
                        ).first()
                        if uncertain_log:
                            uncertain_log.decision_trace = list(uncertain_log.decision_trace or []) + [{
                                "step": "ledger_shadow",
                                "result": "would_write_submission_unknown",
                                "idempotency_source_id": source_id or decision.send_log_id,
                                "ts": datetime.utcnow().isoformat(),
                            }]
                return decision

            if strategy == "fail":
                break

        # All channels exhausted
        return self._create_failed_log(
            project_id, user_id, recipient, content, channel, fallback_order,
            source_type, source_id, template_id, trace,
            error="All channels exhausted", status="exhausted", **_ctx,
        )

    # ── Pipeline helpers ─────────────────────────────────────────────────

    def _policy_window_expired(
        self,
        next_allowed_at: datetime,
        policy_context: Optional[Dict[str, Any]],
        existing_send_log_id: Optional[int],
    ) -> bool:
        boundaries: list[datetime] = []
        raw_deadline = (policy_context or {}).get("deadline_at")
        if isinstance(raw_deadline, datetime):
            boundaries.append(raw_deadline.replace(tzinfo=None))
        elif raw_deadline:
            try:
                boundaries.append(
                    datetime.fromisoformat(str(raw_deadline).replace("Z", "+00:00"))
                    .astimezone(dt_timezone.utc)
                    .replace(tzinfo=None)
                )
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid policy deadline %r", raw_deadline)
        if existing_send_log_id:
            expires_at = self.db.query(SendLog.expires_at).filter(
                SendLog.id == existing_send_log_id,
            ).scalar()
            if expires_at:
                boundaries.append(expires_at)
        return bool(boundaries and next_allowed_at > min(boundaries))

    def _record_ledger_once(
        self,
        *,
        project_id: int,
        user_id: int,
        channel: str,
        source_type: str,
        source_id: Optional[int],
        send_log_id: int,
        sent_at: Optional[datetime],
    ) -> None:
        """Write the authoritative ledger entry exactly once at the I/O boundary.

        The immutable SendLog id is the idempotency key.  A logical caller id
        (for example an EventAction id) is deliberately not suitable: the same
        action can submit many messages over its lifetime.
        """
        try:
            from app.services.scoring.policy_service import PolicyService

            PolicyService(self.db).record_contact_once(
                project_id=project_id,
                user_id=user_id,
                channel=channel,
                source=source_type,
                source_id=send_log_id,
                sent_at=sent_at,
            )
        except Exception as exc:
            logger.warning("Error recording authoritative contact ledger: %s", exc)

    def _get_send_config(self, project_id: int) -> Optional[ProjectSendConfig]:
        return self.db.query(ProjectSendConfig).filter(
            ProjectSendConfig.project_id == project_id,
        ).first()

    def _resolve_channels(
        self,
        preferred: Optional[str],
        fallback_order: Optional[List[str]],
        send_config: Optional[ProjectSendConfig],
        project_id: Optional[int] = None,
    ) -> List[str]:
        """Build ordered channel list, filtered by project-level availability."""
        channels: List[str] = []
        available = set(ChannelRegistry.available_channels())

        # Filter by project-level channel toggles
        if project_id:
            from app.services.channels.channel_registry_service import ChannelRegistryService
            reg = ChannelRegistryService(self.db)
            available = {ch for ch in available if reg.is_channel_available(project_id, ch)}

        if preferred and preferred in available:
            channels.append(preferred)

        fb = fallback_order
        if not fb and send_config and send_config.default_fallback_order:
            fb = send_config.default_fallback_order
        if not fb:
            fb = list(available)

        for ch in fb:
            if ch in available and ch not in channels:
                channels.append(ch)

        return channels

    def _consent_filter(
        self,
        project_id: int,
        user_id: int,
        channels: List[str],
        trace: List[Dict[str, Any]],
    ) -> List[str]:
        """0B consent gate for the Promotional lane.

        Shadow by default: records would-suppress channels in the trace and
        logs them, but returns the channel list UNCHANGED so nothing is
        actually suppressed. Set SEND_CONSENT_ENFORCE truthy to drop
        non-consented channels — only after grandfathering consent_channels
        (hazard H2: enforcing cold suppresses nearly all WA/SMS promo).
        """
        from app.models.messaging import MessagingUser
        from app.services.engine_rollout_service import effective_mode
        from app.services.messaging.consent_manager import consent_manager

        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
        ).first()
        if not user:
            return channels

        enforce = effective_mode(self.db, project_id, "consent") == "enforce"
        suppressed = [
            ch for ch in channels
            if not consent_manager.get_channel_consent(user, ch)
        ]
        if suppressed:
            trace.append({
                "step": "consent",
                "lane": "promotional",
                "no_consent_channels": suppressed,
                "enforced": enforce,
                "ts": datetime.utcnow().isoformat(),
            })
            if not enforce:
                logger.info(
                    "[consent][shadow] user %s WOULD lose promo channels %s (no consent)",
                    user_id, suppressed,
                )
        if enforce:
            return [ch for ch in channels if ch not in suppressed]
        return channels

    def _filter_opt_out(
        self,
        project_id: int,
        user_id: Optional[int],
        channels: List[str],
        trace: List[Dict[str, Any]],
    ) -> List[str]:
        """Remove channels the user has opted out of."""
        if not user_id:
            trace.append({
                "step": "opt_out_check",
                "result": "skipped",
                "reason": "no_user_id",
                "ts": datetime.utcnow().isoformat(),
            })
            return channels

        from app.models.messaging import MessagingUser
        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()

        if not user:
            trace.append({
                "step": "opt_out_check",
                "result": "skipped",
                "reason": "user_not_found",
                "ts": datetime.utcnow().isoformat(),
            })
            return channels

        if user.global_opt_out:
            trace.append({
                "step": "opt_out_check",
                "result": "global_opt_out",
                "ts": datetime.utcnow().isoformat(),
            })
            return []

        # Manual unsubscribe (is_subscribed=False) blocks like a global
        # opt-out — the operator/DSAR toggle must actually silence sends,
        # not just flip a display badge.
        if user.is_subscribed is False:
            trace.append({
                "step": "opt_out_check",
                "result": "unsubscribed",
                "ts": datetime.utcnow().isoformat(),
            })
            return []

        opted_out = set(user.opted_out_channels or [])
        filtered = [ch for ch in channels if ch not in opted_out]

        if len(filtered) < len(channels):
            trace.append({
                "step": "opt_out_check",
                "result": "filtered",
                "removed": list(set(channels) - set(filtered)),
                "ts": datetime.utcnow().isoformat(),
            })
        else:
            trace.append({
                "step": "opt_out_check",
                "result": "ok",
                "ts": datetime.utcnow().isoformat(),
            })

        return filtered

    def _campaign_submission_gate(
        self,
        *,
        project_id: int,
        user_id: int | None,
        source_id: int | None,
        send_log_id: int,
        channel: str,
        policy_context: Dict[str, Any],
    ) -> tuple[bool, str]:
        """Final exact campaign gate after the durable pre-I/O commit.

        Rows are locked through the provider call, serializing cancellation
        and identity/permission changes with the submission boundary.
        """
        if not user_id or not source_id:
            return False, "campaign_submission_context_missing"
        from app.models.campaigns import (
            Campaign,
            CampaignRecipient,
            CampaignRun,
            ContactEndpoint,
            ContactPermissionEvidence,
        )
        from app.models.messaging import ContactVerificationState, MessagingUser

        recipient = self.db.query(CampaignRecipient).filter(
            CampaignRecipient.id == source_id,
            CampaignRecipient.project_id == project_id,
            CampaignRecipient.user_id == user_id,
        ).with_for_update().first()
        if not recipient:
            return False, "campaign_recipient_missing"
        if recipient.status != "processing":
            return False, (
                "campaign_recipient_canceled"
                if recipient.status == "canceled"
                else "campaign_recipient_not_dispatchable"
            )
        run = self.db.query(CampaignRun).filter(
            CampaignRun.id == recipient.run_id,
            CampaignRun.project_id == project_id,
        ).with_for_update().first()
        if not run or run.cancel_requested or run.status in {"canceled", "cancel_requested"}:
            return False, "campaign_run_canceled"
        campaign = self.db.query(Campaign).filter(
            Campaign.id == run.campaign_id,
            Campaign.project_id == project_id,
        ).with_for_update().first()
        if not campaign:
            return False, "campaign_not_found"
        if campaign.status == "paused":
            return False, "campaign_paused"
        if campaign.status != "active":
            return False, "campaign_not_active"
        log = self.db.query(SendLog).filter(
            SendLog.id == send_log_id,
            SendLog.project_id == project_id,
            SendLog.source_type == "campaign",
            SendLog.source_id == source_id,
            SendLog.status == "submitting",
        ).with_for_update().first()
        if not log:
            return False, "campaign_submission_intent_not_active"

        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).with_for_update().first()
        if (
            not user
            or user.status != "active"
            or bool(user.is_sandbox)
            or bool(user.is_blocked)
            or bool(user.global_opt_out)
            or user.is_subscribed is False
            or channel in set(user.opted_out_channels or [])
        ):
            return False, "campaign_contact_not_sendable"
        if bool(user.automations_paused):
            return False, (
                "campaign_contact_paused_hold"
                if user.automations_pause_mode == "hold"
                else "campaign_contact_not_sendable"
            )

        endpoint_id = policy_context.get("endpoint_id")
        endpoint_hash = str(policy_context.get("endpoint_hash") or "")
        endpoint = self.db.query(ContactEndpoint).filter(
            ContactEndpoint.id == endpoint_id,
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == user_id,
            ContactEndpoint.endpoint_type == ("email" if channel == "email" else "phone"),
            ContactEndpoint.status == "active",
            ContactEndpoint.value_hash == endpoint_hash,
        ).with_for_update().first()
        if not endpoint or endpoint.id != recipient.endpoint_id or endpoint_hash != recipient.endpoint_hash:
            return False, "campaign_identifier_changed"

        verification_type = "email" if channel == "email" else "whatsapp"
        state = self.db.query(ContactVerificationState).filter(
            ContactVerificationState.project_id == project_id,
            ContactVerificationState.user_id == user_id,
            ContactVerificationState.endpoint_id == endpoint.id,
            ContactVerificationState.verification_type == verification_type,
            ContactVerificationState.identifier_hash == endpoint_hash,
        ).with_for_update().first()
        now = datetime.utcnow()
        if (
            not state
            or state.canonical_status != "valid"
            or state.last_attempt_status != "succeeded"
            or not state.expires_at
            or state.expires_at <= now
        ):
            return False, "campaign_verification_not_fresh_valid"

        permission_mode = str(policy_context.get("permission_policy") or "explicit_consent")
        if permission_mode in {"explicit_consent", "documented_relationship"}:
            evidence = self.db.query(ContactPermissionEvidence).filter(
                ContactPermissionEvidence.id == policy_context.get("permission_evidence_id"),
                ContactPermissionEvidence.project_id == project_id,
                ContactPermissionEvidence.user_id == user_id,
                ContactPermissionEvidence.endpoint_id == endpoint.id,
                ContactPermissionEvidence.channel == channel,
                ContactPermissionEvidence.permission_type == "marketing",
                ContactPermissionEvidence.status == "granted",
            ).with_for_update().first()
            if not evidence or (evidence.expires_at and evidence.expires_at <= now):
                return False, "campaign_permission_evidence_not_active"
            if permission_mode == "documented_relationship" and (
                not evidence.source
                or not evidence.evidence_ref
                or not (evidence.evidence_metadata or {}).get("legal_basis")
            ):
                return False, "campaign_documented_relationship_not_auditable"
        elif permission_mode == "subscribed_no_optout":
            if not bool(user.is_subscribed):
                return False, "campaign_contact_not_subscribed"
        else:
            return False, "campaign_permission_mode_invalid"
        return True, "ok"

    def _filter_verification_policy(
        self,
        project_id: int,
        user_id: Optional[int],
        channels: List[str],
        trace: List[Dict[str, Any]],
        policy_profile: Optional[str] = None,
        policy_context: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        if not user_id:
            trace.append({
                "step": "contact_verification",
                "result": "skipped",
                "reason": "no_user_id",
                "ts": datetime.utcnow().isoformat(),
            })
            return channels

        from app.models import Project
        from app.models.messaging import (
            ContactVerificationState,
            MessagingUser,
            ProjectVerificationSettings,
        )
        from app.services.messaging.pii_hasher import pii_hasher

        user = self.db.query(MessagingUser).filter(
            MessagingUser.id == user_id,
            MessagingUser.project_id == project_id,
        ).first()
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not user or not project:
            return [] if policy_profile == "campaign" else channels

        settings = self.db.query(ProjectVerificationSettings).filter(
            ProjectVerificationSettings.project_id == project_id,
        ).first()
        now = datetime.utcnow()
        salt = project.pii_salt
        removed: List[Dict[str, Any]] = []
        filtered = list(channels)

        for channel_name, verification_type in (("email", "email"), ("whatsapp", "whatsapp")):
            if channel_name not in filtered:
                continue
            policy = (
                getattr(settings, f"{verification_type}_send_policy")
                if settings else "block_invalid"
            )
            if policy_profile == "campaign":
                policy = "require_fresh_valid"
            if policy == "report_only":
                continue
            state_query = self.db.query(ContactVerificationState).filter(
                ContactVerificationState.project_id == project_id,
                ContactVerificationState.user_id == user_id,
                ContactVerificationState.verification_type == verification_type,
            )
            endpoint_id = (policy_context or {}).get("endpoint_id") if policy_profile == "campaign" else None
            endpoint_hash = (policy_context or {}).get("endpoint_hash") if policy_profile == "campaign" else None
            if endpoint_id is not None:
                state_query = state_query.filter(ContactVerificationState.endpoint_id == endpoint_id)
            state = state_query.order_by(ContactVerificationState.checked_at.desc()).first()
            if not state or state.last_attempt_status != "succeeded" or state.expires_at <= now or not salt:
                if policy_profile == "campaign":
                    filtered.remove(channel_name)
                    removed.append({
                        "channel": channel_name,
                        "status": "missing_or_stale",
                        "policy": policy,
                    })
                continue
            if endpoint_id is not None:
                from app.models.campaigns import ContactEndpoint
                endpoint = self.db.query(ContactEndpoint).filter(
                    ContactEndpoint.id == endpoint_id,
                    ContactEndpoint.project_id == project_id,
                    ContactEndpoint.user_id == user_id,
                    ContactEndpoint.endpoint_type == ("email" if verification_type == "email" else "phone"),
                    ContactEndpoint.status == "active",
                ).first()
                current_hash = endpoint.value_hash if endpoint else None
                if endpoint_hash and current_hash != endpoint_hash:
                    current_hash = None
            else:
                identifier = (
                    pii_hasher.normalize_email(user.email or "")
                    if verification_type == "email"
                    else pii_hasher.normalize_phone(user.phone_e164 or user.phone or "")
                )
                current_hash = (
                    pii_hasher.hash_email(identifier, salt)
                    if verification_type == "email"
                    else pii_hasher.hash_phone(identifier, salt)
                ) if identifier else None
            if not current_hash or current_hash != state.identifier_hash:
                if policy_profile == "campaign":
                    filtered.remove(channel_name)
                    removed.append({
                        "channel": channel_name,
                        "status": "identifier_changed",
                        "policy": policy,
                    })
                continue
            blocked = (
                state.canonical_status != "valid"
                if policy == "require_fresh_valid"
                else state.canonical_status in (
                    {"invalid", "risky"}
                    if policy == "block_invalid_and_risky"
                    else {"invalid"}
                )
            )
            if blocked:
                filtered.remove(channel_name)
                removed.append({
                    "channel": channel_name,
                    "status": state.canonical_status,
                    "provider": state.provider,
                    "policy": policy,
                })

        trace.append({
            "step": "contact_verification",
            "result": "filtered" if removed else "ok",
            "removed": removed,
            "ts": now.isoformat(),
        })
        return filtered

    def _is_quiet_hours(self, config: ProjectSendConfig) -> bool:
        """Check if current time falls within quiet hours."""
        if not config.quiet_hours_start or not config.quiet_hours_end:
            return False

        try:
            tz = ZoneInfo(config.quiet_hours_timezone or "UTC")
        except Exception:
            tz = dt_timezone.utc

        now = datetime.now(tz)
        start_h, start_m = map(int, config.quiet_hours_start.split(":"))
        end_h, end_m = map(int, config.quiet_hours_end.split(":"))

        start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        if start <= end:
            return start <= now <= end
        else:
            # Crosses midnight (e.g. 22:00 → 08:00)
            return now >= start or now <= end

    def _quiet_hours_resume_time(self, config: ProjectSendConfig) -> datetime:
        """Calculate when quiet hours end."""
        try:
            tz = ZoneInfo(config.quiet_hours_timezone or "UTC")
        except Exception:
            tz = dt_timezone.utc

        now = datetime.now(tz)
        end_h, end_m = map(int, config.quiet_hours_end.split(":"))
        resume = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        if resume <= now:
            resume += timedelta(days=1)

        return resume.astimezone(dt_timezone.utc).replace(tzinfo=None)

    def _check_rate_limit(
        self,
        project_id: int,
        recipient: str,
        channel: str,
        max_messages: int = 10,
        window_minutes: int = 5,
    ) -> bool:
        """Returns True if rate-limited."""
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=window_minutes)

        window = self.db.query(ContactRateWindow).filter(
            ContactRateWindow.project_id == project_id,
            ContactRateWindow.contact_identifier == recipient,
            ContactRateWindow.channel == channel,
            ContactRateWindow.window_start > cutoff,
        ).first()

        if window and window.message_count >= max_messages:
            return True

        # Increment or create — actual increment happens after successful send
        return False

    def _increment_rate_counter(
        self, project_id: int, recipient: str, channel: str,
    ) -> None:
        """Increment rate counter after successful send."""
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=5)

        window = self.db.query(ContactRateWindow).filter(
            ContactRateWindow.project_id == project_id,
            ContactRateWindow.contact_identifier == recipient,
            ContactRateWindow.channel == channel,
            ContactRateWindow.window_start > cutoff,
        ).first()

        if window:
            window.message_count += 1
        else:
            self.db.add(ContactRateWindow(
                project_id=project_id,
                contact_identifier=recipient,
                channel=channel,
                window_start=now,
                message_count=1,
            ))
        self.db.flush()

    @staticmethod
    def _lane_priority(lane) -> int:
        """Map the outbound Lane to a SendLog dispatch priority so the pace
        governor's delayed queue drains protected lanes first (transactional
        jumps ahead of promotional when both are waiting for a domain's slots)."""
        from app.services.channels.lanes import Lane
        return {
            Lane.TRANSACTIONAL: 20,
            Lane.CONVERSATIONAL: 10,
            Lane.MANUAL: 0,
            Lane.PROMOTIONAL: 0,
        }.get(lane, 0)

    # ── Log creation helpers ─────────────────────────────────────────────

    def _park_as_candidate(
        self,
        project_id, user_id, recipient, content, channel,
        fallback_order, source_type, source_id, template_id, trace, _ctx,
    ) -> "SendDecision":
        """Park a promotional send as a durable candidate (Phase 2). Stores
        content for late dispatch; the selection sweep promotes the per-contact
        winner. Due immediately (scheduled_at=now) so it is contested on the
        next worker cycle; expires after the candidate TTL if never selected."""
        from app.services.channels.selection import candidate_ttl_minutes
        now = datetime.utcnow()
        expires = now + timedelta(minutes=candidate_ttl_minutes(self.db, project_id))
        trace.append({
            "step": "candidate_parked",
            "expires_at": expires.isoformat(),
            "ts": now.isoformat(),
        })
        log = self._create_send_log(
            project_id=project_id,
            user_id=user_id,
            channel=channel or "",
            recipient=recipient,
            content=content,
            preferred_channel=channel,
            fallback_order=fallback_order,
            source_type=source_type,
            source_id=source_id,
            template_id=template_id,
            status="candidate",
            decision_trace=trace,
            scheduled_at=now,
            render_context=_ctx.get("render_context"),
            deferred_source_enrollment_id=_ctx.get("deferred_source_enrollment_id"),
            retry_of_id=_ctx.get("retry_of_id"),
            slot_id=_ctx.get("slot_id"),
            sending_domain=_ctx.get("sending_domain"),
        )
        log.expires_at = expires
        self.db.flush()
        self._refresh_future_attention(log, trace)
        return SendDecision(
            success=True, send_log_id=log.id, status="candidate", decision_trace=trace,
        )

    def _link_tracking_channels(self, project_id: int) -> list:
        """Channels (beyond email) with outbound link rewriting enabled."""
        try:
            from app.models import MESConfig
            config = self.db.query(MESConfig.link_tracking_channels).filter(
                MESConfig.project_id == project_id,
            ).first()
            if config and config[0]:
                return [str(c).lower() for c in config[0]]
        except Exception as e:
            logger.warning("Error loading link_tracking_channels: %s", e)
        return []

    def _find_recent_whatsapp_duplicate(
        self,
        project_id: int,
        user_id: Optional[int],
        recipient: str,
        content: OutboundContent,
        channel: str,
        source_type: str,
        template_id: Optional[int],
        existing_send_log_id: Optional[int],
        dry_run: bool,
    ) -> Optional[SendLog]:
        """Return a recent equivalent automated WhatsApp send, if any.

        This is intentionally narrow: it does not affect manual sends or worker
        dispatch of an existing row. It catches accidental duplicate execution
        of the same automated intent before a second provider call is made.
        """
        if (
            dry_run
            or existing_send_log_id is not None
            or channel != "whatsapp"
            or source_type == "manual"
        ):
            return None

        fingerprint = self._content_fingerprint(content)
        lock_key = (
            f"wa-send:{project_id}:{user_id or 'anon'}:{recipient}:"
            f"{source_type}:{template_id or 'none'}:{fingerprint}"
        )
        try:
            bind = self.db.get_bind()
            if bind is not None and bind.dialect.name == "postgresql":
                self.db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {"lock_key": lock_key},
                )
        except Exception as exc:
            logger.warning("WhatsApp duplicate guard lock unavailable: %s", exc)

        cutoff = datetime.utcnow() - timedelta(minutes=10)
        candidates = (
            self.db.query(SendLog)
            .filter(
                SendLog.project_id == project_id,
                SendLog.channel == "whatsapp",
                SendLog.recipient == recipient,
                SendLog.source_type == source_type,
                SendLog.template_id == template_id,
                SendLog.status.in_(("queued", "sent", "delivered", "read")),
                SendLog.queued_at >= cutoff,
            )
            .order_by(SendLog.id.desc())
            .limit(20)
            .all()
        )
        for candidate in candidates:
            if user_id and candidate.user_id and candidate.user_id != user_id:
                continue
            if self._payload_fingerprint(candidate.content_payload, candidate.content_summary) == fingerprint:
                return candidate
        return None

    def _content_fingerprint(self, content: OutboundContent) -> str:
        try:
            payload = content.to_dict()
        except Exception:
            payload = {"summary": content.summary()}
        return self._payload_fingerprint(payload, None)

    def _payload_fingerprint(self, payload: Optional[dict], summary: Optional[str]) -> str:
        body = payload if payload is not None else {"summary": summary or ""}
        encoded = json.dumps(body, sort_keys=True, default=str, ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _intent_fields(
        self,
        source_type: str,
        project_id: Optional[int] = None,
        source_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Resolve lane plus the persisted, authored attention identity."""
        if project_id is not None:
            try:
                from app.services.channels.source_contract import resolve_dispatch_source

                return dict(resolve_dispatch_source(
                    self.db,
                    project_id,
                    source_type,
                    source_id,
                    require_runtime_modes=False,
                ).intent_fields)
            except Exception:
                # Failed/disabled source attempts are still logged below.  The
                # permissive fallback captures whatever identity is available
                # without turning log creation into a second dispatch gate.
                pass
        from app.services.channels.lanes import intent_fields_for, resolve_lane_for_send
        lane, _ = resolve_lane_for_send(
            self.db, project_id, source_type, source_id,
        ) if project_id is not None else (None, False)
        fields = intent_fields_for(source_type, declared_lane=lane)
        fields.update({
            "attention_scope": f"contact.{lane.value}" if lane is not None else None,
            "purpose_key": None,
            "attention_policy_snapshot": None,
        })
        if project_id is None:
            return fields

        policy = None
        purpose_key = None
        try:
            if source_type == "funnel" and source_id is not None:
                from app.models import Funnel
                row = self.db.query(
                    Funnel.attention_policy, Funnel.purpose_key,
                ).filter(
                    Funnel.id == source_id,
                    Funnel.project_id == project_id,
                ).first()
            elif source_type == "event_action" and source_id is not None:
                from app.models import EventAction
                row = self.db.query(
                    EventAction.attention_policy, EventAction.purpose_key,
                ).filter(
                    EventAction.id == source_id,
                    EventAction.project_id == project_id,
                ).first()
            elif source_type == "campaign" and source_id is not None:
                from app.models.campaigns import Campaign, CampaignRecipient, CampaignRun
                row = self.db.query(
                    Campaign.attention_policy, Campaign.purpose_key,
                ).join(
                    CampaignRun, CampaignRun.campaign_id == Campaign.id,
                ).join(
                    CampaignRecipient, CampaignRecipient.run_id == CampaignRun.id,
                ).filter(
                    CampaignRecipient.id == source_id,
                    CampaignRecipient.project_id == project_id,
                ).first()
            elif source_type == "template" and source_id is not None:
                from app.models.messaging import MessagingTemplate
                row = self.db.query(
                    MessagingTemplate.attention_policy,
                    MessagingTemplate.purpose_key,
                ).filter(
                    MessagingTemplate.id == source_id,
                    MessagingTemplate.project_id == project_id,
                ).first()
            elif source_type == "retry" and source_id is not None:
                from app.models import SendLog
                previous = self.db.query(SendLog).filter(
                    SendLog.id == source_id,
                    SendLog.project_id == project_id,
                ).first()
                if previous:
                    fields["attention_scope"] = previous.attention_scope
                    fields["purpose_key"] = previous.purpose_key
                    fields["attention_policy_snapshot"] = (
                        dict(previous.attention_policy_snapshot)
                        if isinstance(previous.attention_policy_snapshot, dict)
                        else None
                    )
                    if previous.intent_tier is not None:
                        fields["intent_tier"] = previous.intent_tier
                return fields
            else:
                row = None
            if row:
                policy, purpose_key = row
        except Exception as exc:
            logger.warning("Could not resolve authored attention policy: %s", exc)

        if isinstance(policy, dict):
            fields["attention_policy_snapshot"] = dict(policy)
            scope = policy.get("attention_scope")
            ordinal = policy.get("ordinal")
            if isinstance(scope, str) and scope:
                fields["attention_scope"] = scope
            if isinstance(ordinal, int):
                fields["intent_tier"] = ordinal
        if purpose_key:
            fields["purpose_key"] = purpose_key
        return fields

    def _create_send_log(self, **kwargs) -> SendLog:
        content: OutboundContent = kwargs.pop("content", None)
        from app.services.channels.selection import (
            ATTENTION_CONTENDER_STATUSES,
            acquire_attention_xact_lock,
        )

        # Dispatch-in-place: finalize an already-parked deferred/delayed row
        # instead of inserting a new one (avoids the double-SendLog where the
        # worker flips the original to 'sent' AND the pipeline mints a second).
        existing_id = kwargs.get("existing_send_log_id")
        if existing_id:
            existing = self.db.query(SendLog).filter(SendLog.id == existing_id).first()
            if existing is not None:
                next_status = kwargs.get("status", existing.status)
                if (
                    existing.status in ATTENTION_CONTENDER_STATUSES
                    or next_status in ATTENTION_CONTENDER_STATUSES
                ):
                    # Lock order is always attention slot -> SendLog row. It
                    # closes the insertion race between final selection and
                    # dispatch-in-place, and matches CampaignWorker's handoff.
                    acquire_attention_xact_lock(
                        self.db,
                        existing.project_id,
                        existing.user_id,
                        existing.recipient,
                        existing.attention_scope,
                    )
                    existing = self.db.query(SendLog).filter(
                        SendLog.id == existing_id,
                    ).with_for_update().first()
                    if existing is None:
                        raise RuntimeError(
                            "Existing SendLog disappeared during attention lock"
                        )
                if content is not None:
                    existing.content_type = content.content_type
                    existing.content_summary = content.summary()
                    existing.content_payload = content.to_dict()
                _ch = kwargs.get("channel")
                if _ch:
                    existing.channel = _ch
                    existing.resolved_channel = _ch
                existing.status = kwargs.get("status", existing.status)
                if kwargs.get("provider_message_id") is not None:
                    existing.provider_message_id = kwargs.get("provider_message_id")
                if kwargs.get("provider_response") is not None:
                    existing.provider_response = kwargs.get("provider_response")
                if kwargs.get("error_message") is not None:
                    existing.error_message = kwargs.get("error_message")
                if kwargs.get("decision_trace") is not None:
                    existing.decision_trace = kwargs.get("decision_trace")
                if "fallback_attempt" in kwargs:
                    existing.fallback_attempt = kwargs.get("fallback_attempt")
                if kwargs.get("scheduled_at") is not None:
                    existing.scheduled_at = kwargs.get("scheduled_at")
                if kwargs.get("sent_at") is not None:
                    existing.sent_at = kwargs.get("sent_at")
                if kwargs.get("failed_at") is not None:
                    existing.failed_at = kwargs.get("failed_at")
                if kwargs.get("tracking_token") is not None:
                    existing.tracking_token = kwargs.get("tracking_token")
                if kwargs.get("instance_id") is not None:
                    existing.instance_id = kwargs.get("instance_id")
                if kwargs.get("sending_domain") is not None:
                    existing.sending_domain = kwargs.get("sending_domain")
                self.db.flush()
                return existing

        intent_fields = self._intent_fields(
            kwargs.get("source_type", "manual"),
            kwargs.get("project_id"),
            kwargs.get("source_id"),
        )
        if kwargs.get("status", "queued") in ATTENTION_CONTENDER_STATUSES:
            # Every contender enters the ledger through the same slot lock.
            # A final dispatcher holding that lock therefore establishes a
            # deterministic before/after boundary for concurrent arrivals.
            acquire_attention_xact_lock(
                self.db,
                kwargs.get("project_id"),
                kwargs.get("user_id"),
                kwargs.get("recipient"),
                intent_fields.get("attention_scope"),
            )
        log = SendLog(
            project_id=kwargs.get("project_id"),
            user_id=kwargs.get("user_id"),
            channel=kwargs.get("channel", ""),
            recipient=kwargs.get("recipient", ""),
            content_type=content.content_type if content else "text",
            content_summary=content.summary() if content else None,
            content_payload=content.to_dict() if content else None,
            template_id=kwargs.get("template_id"),
            source_type=kwargs.get("source_type", "manual"),
            source_id=kwargs.get("source_id"),
            decision_trace=kwargs.get("decision_trace"),
            preferred_channel=kwargs.get("preferred_channel"),
            resolved_channel=kwargs.get("channel"),
            fallback_order=kwargs.get("fallback_order"),
            fallback_attempt=kwargs.get("fallback_attempt", 0),
            status=kwargs.get("status", "queued"),
            provider_message_id=kwargs.get("provider_message_id"),
            provider_response=kwargs.get("provider_response"),
            error_message=kwargs.get("error_message"),
            scheduled_at=kwargs.get("scheduled_at"),
            sent_at=kwargs.get("sent_at"),
            failed_at=kwargs.get("failed_at"),
            tracking_token=kwargs.get("tracking_token"),
            instance_id=kwargs.get("instance_id"),
            render_context=kwargs.get("render_context"),
            deferred_source_enrollment_id=kwargs.get("deferred_source_enrollment_id"),
            retry_of_id=kwargs.get("retry_of_id"),
            slot_id=kwargs.get("slot_id"),
            sending_domain=kwargs.get("sending_domain"),
            priority=kwargs.get("priority", 0),
            **intent_fields,
        )
        self.db.add(log)
        self.db.flush()
        return log

    def _create_failed_log(
        self,
        project_id: int,
        user_id: Optional[int],
        recipient: str,
        content: OutboundContent,
        preferred_channel: Optional[str],
        fallback_order: Optional[List[str]],
        source_type: str,
        source_id: Optional[int],
        template_id: Optional[int],
        trace: List[Dict[str, Any]],
        error: str,
        status: str = "failed",
        render_context: Optional[Dict[str, Any]] = None,
        deferred_source_enrollment_id: Optional[int] = None,
        retry_of_id: Optional[int] = None,
        existing_send_log_id: Optional[int] = None,
        dry_run: bool = False,
        slot_id: Optional[str] = None,
        sending_domain: Optional[str] = None,
        priority: int = 0,
    ) -> SendDecision:
        trace.append({
            "step": "final",
            "result": status,
            "error": error,
            "ts": datetime.utcnow().isoformat(),
        })
        if dry_run:
            return SendDecision(
                success=False, status=status, error=error, decision_trace=trace,
            )
        log = self._create_send_log(
            project_id=project_id,
            user_id=user_id,
            channel=preferred_channel or "",
            recipient=recipient,
            content=content,
            preferred_channel=preferred_channel,
            fallback_order=fallback_order,
            source_type=source_type,
            source_id=source_id,
            template_id=template_id,
            status=status,
            error_message=error,
            decision_trace=trace,
            failed_at=datetime.utcnow(),
            render_context=render_context,
            deferred_source_enrollment_id=deferred_source_enrollment_id,
            retry_of_id=retry_of_id,
            existing_send_log_id=existing_send_log_id,
            slot_id=slot_id,
            sending_domain=sending_domain,
        )
        # Emit channel.{ch}.failed event
        if status == "failed":
            self._emit_channel_event(log, "failed", error_message=error)
        return SendDecision(
            success=False,
            send_log_id=log.id,
            status=status,
            error=error,
            decision_trace=trace,
        )

    def _create_scheduled_log(
        self,
        project_id: int,
        user_id: Optional[int],
        recipient: str,
        content: OutboundContent,
        preferred_channel: Optional[str],
        fallback_order: Optional[List[str]],
        source_type: str,
        source_id: Optional[int],
        template_id: Optional[int],
        trace: List[Dict[str, Any]],
        scheduled_at: datetime,
        render_context: Optional[Dict[str, Any]] = None,
        deferred_source_enrollment_id: Optional[int] = None,
        retry_of_id: Optional[int] = None,
        existing_send_log_id: Optional[int] = None,
        dry_run: bool = False,
        slot_id: Optional[str] = None,
        sending_domain: Optional[str] = None,
        priority: int = 0,
    ) -> SendDecision:
        if dry_run:
            return SendDecision(
                success=True, status="delayed", decision_trace=trace,
            )
        log = self._create_send_log(
            project_id=project_id,
            user_id=user_id,
            channel=preferred_channel or "",
            recipient=recipient,
            content=content,
            preferred_channel=preferred_channel,
            fallback_order=fallback_order,
            source_type=source_type,
            source_id=source_id,
            template_id=template_id,
            status="delayed",
            decision_trace=trace,
            scheduled_at=scheduled_at,
            render_context=render_context,
            deferred_source_enrollment_id=deferred_source_enrollment_id,
            retry_of_id=retry_of_id,
            existing_send_log_id=existing_send_log_id,
            slot_id=slot_id,
            sending_domain=sending_domain,
            priority=priority,
        )
        self._refresh_future_attention(log, trace)
        return SendDecision(
            success=True,
            send_log_id=log.id,
            status="delayed",
            decision_trace=trace,
        )

    def _refresh_future_attention(
        self,
        log: SendLog,
        trace: List[Dict[str, Any]],
    ) -> None:
        """Persist an advisory plan without turning it into send permission."""
        try:
            from app.services.channels.selection import future_plan_mode

            mode = future_plan_mode(self.db, log.project_id)
            if mode == "off":
                return
            from app.services.channels.future_attention_planner import FutureAttentionPlanner

            report = FutureAttentionPlanner(self.db).refresh_for(log)
            own = report.get("consequences_by_id", {}).get(log.id, {})
            trace.append({
                "step": "future_attention_planned",
                "mode": mode,
                "consequence_at_gate": own.get("consequence"),
                "horizon_end": report.get("horizon_end"),
                "external_sends": 0,
                "ts": report.get("evaluated_at"),
            })
            log.decision_trace = list(trace)
            self.db.flush()
        except Exception as exc:
            # Planning must never create an unsafe bypass. Enforce-mode
            # dispatch independently recomputes the contest at the gate.
            logger.warning("Could not refresh future attention plan: %s", exc)

    def _emit_channel_event(
        self,
        send_log: SendLog,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Emit a channel.<channel>.<status> MessagingEvent for the automation pipeline."""
        channel = send_log.channel or "unknown"
        event_name = f"channel.{channel}.{status}"

        properties: Dict[str, Any] = {
            "send_log_id": send_log.id,
            "channel": channel,
            "recipient": send_log.recipient,
            "template_id": send_log.template_id,
            "source_type": send_log.source_type,
            "source_id": send_log.source_id,
            "provider_message_id": send_log.provider_message_id,
            "previous_status": "queued",
            "instance_id": send_log.instance_id,
            "arm_id": f"{channel}:{send_log.source_type}:{send_log.template_id or 'none'}",
        }
        if error_message:
            properties["error_message"] = error_message

        event = MessagingEvent(
            project_id=send_log.project_id,
            user_id=send_log.user_id,
            event_name=event_name,
            source="send_service",
            properties=properties,
        )
        self.db.add(event)
        self.db.flush()
