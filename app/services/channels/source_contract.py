"""Fail-closed source registration and authored-attention dispatch gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.services.channels.lanes import (
    AttentionParticipation,
    Lane,
    OutboundSourceContract,
    OutboundSourceContractError,
    default_intent_tier,
    require_source_contract,
    resolve_lane_for_send,
)
from app.schemas.orchestration_attention import AttentionPolicyInput


@dataclass(frozen=True)
class DispatchSourceResolution:
    contract: OutboundSourceContract
    lane: Lane
    intent_fields: dict[str, Any]


def _authored_row(
    db: Session,
    project_id: int,
    source_type: str,
    source_id: Optional[int],
) -> Any:
    if source_id is None:
        return None
    if source_type == "funnel":
        from app.models import Funnel

        return db.query(Funnel.attention_policy, Funnel.purpose_key).filter(
            Funnel.id == source_id,
            Funnel.project_id == project_id,
            Funnel.status == "active",
        ).first()
    if source_type == "event_action":
        from app.models import EventAction

        return db.query(EventAction.attention_policy, EventAction.purpose_key).filter(
            EventAction.id == source_id,
            EventAction.project_id == project_id,
            EventAction.is_active.is_(True),
        ).first()
    if source_type == "campaign":
        from app.models.campaigns import Campaign, CampaignRecipient, CampaignRun

        return db.query(Campaign.attention_policy, Campaign.purpose_key).join(
            CampaignRun, CampaignRun.campaign_id == Campaign.id,
        ).join(
            CampaignRecipient, CampaignRecipient.run_id == CampaignRun.id,
        ).filter(
            CampaignRecipient.id == source_id,
            CampaignRecipient.project_id == project_id,
            Campaign.status == "active",
        ).first()
    if source_type == "template":
        from app.models.messaging import MessagingTemplate

        return db.query(
            MessagingTemplate.attention_policy,
            MessagingTemplate.purpose_key,
        ).filter(
            MessagingTemplate.id == source_id,
            MessagingTemplate.project_id == project_id,
            MessagingTemplate.is_active.is_(True),
            MessagingTemplate.automation_enabled.is_(True),
        ).first()
    return None


def _retry_fields(
    db: Session,
    project_id: int,
    source_id: Optional[int],
) -> dict[str, Any]:
    from app.models import SendLog

    if source_id is None:
        raise OutboundSourceContractError("retry source_id must reference the previous SendLog")
    previous = db.query(SendLog).filter(
        SendLog.id == source_id,
        SendLog.project_id == project_id,
    ).first()
    if previous is None:
        raise OutboundSourceContractError("retry source_id does not resolve inside the project")
    if not previous.intent_class:
        raise OutboundSourceContractError("retry source has no durable intent snapshot")
    fields = {
        "intent_class": previous.intent_class,
        "intent_tier": previous.intent_tier,
        "attention_scope": previous.attention_scope,
        "purpose_key": previous.purpose_key,
        "attention_policy_snapshot": (
            dict(previous.attention_policy_snapshot)
            if isinstance(previous.attention_policy_snapshot, dict)
            else None
        ),
    }
    if previous.intent_class == Lane.PROMOTIONAL.value and (
        not fields["attention_scope"] or not fields["purpose_key"]
        or fields["intent_tier"] is None
    ):
        raise OutboundSourceContractError(
            "promotional retry has no complete authored attention snapshot"
        )
    return fields


def resolve_dispatch_source(
    db: Session,
    project_id: int,
    source_type: Optional[str],
    source_id: Optional[int],
    *,
    require_runtime_modes: bool = True,
) -> DispatchSourceResolution:
    """Resolve and validate one source before any outbound side effect.

    Unknown sources, incomplete authored identities and promotional automation
    running outside Candidate+Selection all fail closed.
    """
    st = (source_type or "").strip()
    contract = require_source_contract(st)
    lane, known = resolve_lane_for_send(db, project_id, st, source_id)
    if not known:
        raise OutboundSourceContractError(f"Could not resolve registered source_type {st}")

    if st == "retry":
        fields = _retry_fields(db, project_id, source_id)
        try:
            lane = Lane(fields["intent_class"])
        except ValueError as exc:
            raise OutboundSourceContractError("retry intent_class is invalid") from exc
    else:
        fields = {
            "intent_class": lane.value,
            "intent_tier": default_intent_tier(lane),
            "attention_scope": f"contact.{lane.value}",
            "purpose_key": None,
            "attention_policy_snapshot": None,
        }

    participates = (
        lane == Lane.PROMOTIONAL
        and contract.attention_participation
        in {AttentionParticipation.REQUIRED_WHEN_PROMOTIONAL, AttentionParticipation.INHERIT}
    )
    if contract.requires_authored_attention and st != "retry":
        row = _authored_row(db, project_id, st, source_id)
        if row is None:
            raise OutboundSourceContractError(
                f"{st} source_id does not resolve to an enabled authored automation"
            )
        raw_policy, purpose_key = row
        if not raw_policy:
            raise OutboundSourceContractError(f"{st} has no attention_policy")
        if not purpose_key:
            raise OutboundSourceContractError(f"{st} has no purpose_key")
        try:
            policy = AttentionPolicyInput(**raw_policy)
        except Exception as exc:
            raise OutboundSourceContractError(f"{st} attention_policy is invalid: {exc}") from exc
        fields.update({
            "attention_scope": policy.attention_scope,
            "intent_tier": policy.ordinal,
            "purpose_key": purpose_key,
            "attention_policy_snapshot": policy.model_dump(mode="json"),
        })

    if participates and require_runtime_modes:
        from app.services.channels.selection import candidate_mode, selection_mode

        candidate = candidate_mode(db, project_id)
        selection = selection_mode(db, project_id)
        if candidate != "enforce" or selection != "enforce":
            raise OutboundSourceContractError(
                "Promotional automation requires Candidate and Selection in enforce "
                f"(candidate={candidate}, selection={selection})"
            )
    return DispatchSourceResolution(contract=contract, lane=lane, intent_fields=fields)
