"""
Outbound Lanes — classify a send's source_type into one of four lanes that
later phases use to decide arbitrability and consent.

This is the canonical taxonomy the rest of the consolidation builds on:

- TRANSACTIONAL : always sends; bypasses marketing consent (opt-out still applies).
- CONVERSATIONAL: user-initiated replies (chatbot / agent team / support);
  session-window governed; never arbitrated or held; consent not required.
- MANUAL        : explicit human intent; protected from *automatic*
  supersession; cancellable only by a human; governor warns, never blocks.
- PROMOTIONAL   : marketing; fully arbitrated; subject to consent / budget /
  cooldown / quiet hours.

Lane-only inspection still resolves an unknown value to MANUAL for legacy
reporting compatibility. Delivery does not: SendService and deferred intent
creation require a complete registration below and fail closed before any
provider I/O.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Lane(str, Enum):
    TRANSACTIONAL = "transactional"
    CONVERSATIONAL = "conversational"
    MANUAL = "manual"
    PROMOTIONAL = "promotional"


class AttentionParticipation(str, Enum):
    """How a source participates in the shared attention decision plane."""

    FORBIDDEN = "forbidden"
    REQUIRED_WHEN_PROMOTIONAL = "required_when_promotional"
    INHERIT = "inherit"


@dataclass(frozen=True)
class OutboundSourceContract:
    """Registration required before a source may reach outbound delivery.

    ``activation_kind`` binds authored automation definitions to the common
    impact-preview/approval gate.  A deliberate exemption is explicit and
    reviewable; an unknown source string is never an exemption.
    """

    source_type: str
    lane: Lane
    automated: bool
    attention_participation: AttentionParticipation
    activation_kind: Optional[str] = None
    requires_authored_attention: bool = False
    exemption_reason: Optional[str] = None


def _contract(
    source_type: str,
    lane: Lane,
    *,
    automated: bool = False,
    attention: AttentionParticipation = AttentionParticipation.FORBIDDEN,
    activation_kind: Optional[str] = None,
    authored: bool = False,
    exemption_reason: Optional[str] = None,
) -> OutboundSourceContract:
    return OutboundSourceContract(
        source_type=source_type,
        lane=lane,
        automated=automated,
        attention_participation=attention,
        activation_kind=activation_kind,
        requires_authored_attention=authored,
        exemption_reason=exemption_reason,
    )


# This registry is the platform boundary. Adding a new caller-side
# ``source_type`` without adding a complete declaration here fails closed in
# SendService and DeferredSendHelper before provider I/O or contender creation.
OUTBOUND_SOURCE_CONTRACTS = {
    # Manual / operator-initiated
    "manual": _contract("manual", Lane.MANUAL),
    "manual_send": _contract("manual_send", Lane.MANUAL),
    "approval": _contract("approval", Lane.MANUAL),
    "email_test": _contract("email_test", Lane.MANUAL),
    "whatsapp_test": _contract("whatsapp_test", Lane.MANUAL),
    "meta_test": _contract("meta_test", Lane.MANUAL),
    "template_test": _contract("template_test", Lane.MANUAL),
    "test_send": _contract("test_send", Lane.MANUAL),
    "external_touch": _contract("external_touch", Lane.MANUAL),
    # Conversational / user-initiated replies
    "chatbot": _contract(
        "chatbot", Lane.CONVERSATIONAL, automated=True,
        exemption_reason="User-initiated conversational reply; session-window governed.",
    ),
    "agent_team": _contract(
        "agent_team", Lane.CONVERSATIONAL, automated=True,
        exemption_reason="User-initiated conversational reply; session-window governed.",
    ),
    "support": _contract(
        "support", Lane.CONVERSATIONAL,
        exemption_reason="Operator-owned support reply.",
    ),
    # Promotional / marketing automation
    "template": _contract(
        "template", Lane.PROMOTIONAL, automated=True,
        attention=AttentionParticipation.REQUIRED_WHEN_PROMOTIONAL,
        activation_kind="template", authored=True,
    ),
    "funnel": _contract(
        "funnel", Lane.PROMOTIONAL, automated=True,
        attention=AttentionParticipation.REQUIRED_WHEN_PROMOTIONAL,
        activation_kind="funnel", authored=True,
    ),
    "event_action": _contract(
        "event_action", Lane.PROMOTIONAL, automated=True,
        attention=AttentionParticipation.REQUIRED_WHEN_PROMOTIONAL,
        activation_kind="event_action", authored=True,
    ),
    # Audience-based campaigns and nurture runs. This source type is always
    # promotional, independent of project-wide migration flags.
    "campaign": _contract(
        "campaign", Lane.PROMOTIONAL, automated=True,
        attention=AttentionParticipation.REQUIRED_WHEN_PROMOTIONAL,
        activation_kind="campaign", authored=True,
    ),
}

SOURCE_TYPE_LANE = {
    source_type: contract.lane
    for source_type, contract in OUTBOUND_SOURCE_CONTRACTS.items()
}

# A retry inherits the durable intent snapshot of the send it retries. If an
# old row has no snapshot, resolution falls back to the chain root's authored
# source. Missing provenance fails closed to PROMOTIONAL: retry must never be
# an implicit consent bypass.
RETRY_SOURCE_TYPE = "retry"


class OutboundSourceContractError(ValueError):
    pass


def source_contract(source_type: Optional[str]) -> Optional[OutboundSourceContract]:
    st = (source_type or "").strip()
    if st == RETRY_SOURCE_TYPE:
        return OutboundSourceContract(
            source_type=RETRY_SOURCE_TYPE,
            lane=Lane.PROMOTIONAL,
            automated=True,
            attention_participation=AttentionParticipation.INHERIT,
            requires_authored_attention=True,
        )
    return OUTBOUND_SOURCE_CONTRACTS.get(st)


def require_source_contract(source_type: Optional[str]) -> OutboundSourceContract:
    contract = source_contract(source_type)
    if contract is None:
        raise OutboundSourceContractError(
            f"Unregistered outbound source_type: {(source_type or '').strip() or '<empty>'}"
        )
    return contract


def validate_source_registry() -> None:
    """Import/test-time invariant for every declared source."""
    activation_kinds: set[str] = set()
    for key, contract in OUTBOUND_SOURCE_CONTRACTS.items():
        if key != contract.source_type:
            raise RuntimeError(f"Outbound source registry key mismatch: {key}")
        if contract.attention_participation == AttentionParticipation.REQUIRED_WHEN_PROMOTIONAL:
            if not contract.automated or not contract.requires_authored_attention:
                raise RuntimeError(f"Attention source {key} lacks authored automation contract")
            if not contract.activation_kind:
                raise RuntimeError(f"Attention source {key} lacks an activation gate")
            activation_kinds.add(contract.activation_kind)
        elif contract.automated and not contract.exemption_reason:
            raise RuntimeError(f"Automated source {key} needs attention participation or an exemption")
    if activation_kinds != {"campaign", "event_action", "funnel", "template"}:
        raise RuntimeError("Outbound activation kinds and impact gate are out of sync")


validate_source_registry()


def resolve_lane(
    source_type: Optional[str],
    root_source_type: Optional[str] = None,
) -> Lane:
    """Resolve a source_type to its Lane.

    root_source_type: for retries, the source_type of the chain root so the
    retry inherits the original send's lane.
    """
    st = (source_type or "").strip()
    if st == RETRY_SOURCE_TYPE:
        if root_source_type:
            return resolve_lane(root_source_type)
        return Lane.PROMOTIONAL
    contract = source_contract(st)
    return contract.lane if contract else Lane.MANUAL


def resolve_lane_for_send(
    db,
    project_id: int,
    source_type: Optional[str],
    source_id: Optional[int],
) -> tuple[Lane, bool]:
    """Resolve lane with an authored, project-scoped Event Action override.

    ``source_id`` is the EventAction id when ``source_type=event_action``.
    For retries it is the prior SendLog id. The prior row's durable
    ``intent_class`` wins; legacy rows fall back to their root source.
    Missing/invalid attribution fails closed to the promotional lane.
    """
    st = (source_type or "").strip()
    if st == RETRY_SOURCE_TYPE:
        if db is None or source_id is None:
            return Lane.PROMOTIONAL, False

        from app.models import SendLog

        current_id = source_id
        visited: set[int] = set()
        for _ in range(32):
            if current_id in visited:
                return Lane.PROMOTIONAL, False
            visited.add(current_id)
            current = db.query(SendLog).filter(
                SendLog.id == current_id,
                SendLog.project_id == project_id,
            ).first()
            if current is None:
                return Lane.PROMOTIONAL, False

            try:
                if current.intent_class:
                    return Lane(str(current.intent_class)), True
            except ValueError:
                pass

            current_source = (current.source_type or "").strip()
            if current_source != RETRY_SOURCE_TYPE:
                return resolve_lane_for_send(
                    db, project_id, current_source, current.source_id,
                )

            parent_id = current.retry_of_id or current.source_id
            if parent_id is None:
                return Lane.PROMOTIONAL, False
            current_id = parent_id

        return Lane.PROMOTIONAL, False

    if st == "event_action" and source_id is not None and db is not None:
        from app.models import EventAction

        declared = db.query(EventAction.lane).filter(
            EventAction.id == source_id,
            EventAction.project_id == project_id,
        ).scalar()
        try:
            if declared:
                return Lane(str(declared)), True
        except ValueError:
            pass
        return Lane.PROMOTIONAL, False
    return resolve_lane(st), is_known_source_type(st)


def is_known_source_type(source_type: Optional[str]) -> bool:
    """True if the source_type is explicitly mapped (i.e. not falling back to
    the MANUAL default). Used to count the unknown long tail in 0C."""
    st = (source_type or "").strip()
    return source_contract(st) is not None


# Default declared-importance ordinal per lane (Phase 1 shadow seed for
# intent_tier). Higher = more important. These mirror the spirit of the
# inbound routing ladder and are a starting point only — the authored
# intent model (later phase) replaces them.
LANE_DEFAULT_TIER = {
    Lane.TRANSACTIONAL: 90,
    Lane.CONVERSATIONAL: 70,
    Lane.MANUAL: 50,
    Lane.PROMOTIONAL: 10,
}


def default_intent_tier(lane: Lane) -> int:
    return LANE_DEFAULT_TIER.get(lane, 50)


def intent_fields_for(
    source_type: Optional[str],
    root_source_type: Optional[str] = None,
    *,
    declared_lane: Optional[Lane] = None,
) -> dict:
    """Phase 1 shadow intent fields for a SendLog: {'intent_class','intent_tier'}.
    Use at every SendLog creation site so the eventual authored intent model
    has parity data. root_source_type lets a retry inherit its chain root's lane."""
    lane = declared_lane or resolve_lane(source_type, root_source_type)
    return {"intent_class": lane.value, "intent_tier": default_intent_tier(lane)}
