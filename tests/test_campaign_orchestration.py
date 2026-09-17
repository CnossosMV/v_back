"""Focused contracts for campaign selection, policy, cadence and dispatch."""

from datetime import datetime, timedelta
import smtplib
from types import SimpleNamespace
from unittest import TestCase

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.campaigns import (
    ContactEndpoint,
    ContactPermissionEvidence,
    ChannelDeliveryProfile,
)
from app.models.messaging import ContactVerificationState, MessagingUser
from app.services.campaigns.capacity import _daily_capacity
from app.services.campaigns.eligibility import (
    CampaignEligibilityService,
    CampaignPolicyError,
    validate_policy,
)
from app.services.campaigns.delivery_profiles import DeliveryProfileService
from app.services.campaigns.policy_overrides import (
    CampaignOverrideError,
    normalize_override_request,
)
from app.services.campaigns.recurrence import next_occurrence
from app.services.contact_groups.compiler import _coerce_datetime, compile_condition
from app.services.email_sender import EmailSender


NOW = datetime(2026, 8, 22, 12, 0, 0)
_ASSERT_RAISES = TestCase().assertRaises


def _user(**overrides):
    values = {
        "id": 20,
        "project_id": 10,
        "external_id": "contact-20",
        "status": "active",
        "is_sandbox": False,
        "is_blocked": False,
        "global_opt_out": False,
        "is_subscribed": True,
        "opted_out_channels": [],
    }
    values.update(overrides)
    return MessagingUser(**values)


def _endpoint(endpoint_id: int, value_hash: str, primary: bool = False, seen_offset: int = 0):
    return ContactEndpoint(
        id=endpoint_id,
        project_id=10,
        user_id=20,
        endpoint_type="email",
        value=f"email-{endpoint_id}@example.test",
        normalized_value=f"email-{endpoint_id}@example.test",
        value_hash=value_hash,
        is_primary=primary,
        status="active",
        source="test",
        first_seen_at=NOW - timedelta(days=100 - seen_offset),
        last_seen_at=NOW - timedelta(days=seen_offset),
    )


def _verification(endpoint_id: int, value_hash: str, status: str = "valid", checked_offset: int = 0):
    return ContactVerificationState(
        id=endpoint_id,
        project_id=10,
        user_id=20,
        endpoint_id=endpoint_id,
        verification_type="email",
        identifier_hash=value_hash,
        provider="millionverifier",
        canonical_status=status,
        checked_at=NOW - timedelta(days=checked_offset),
        expires_at=NOW + timedelta(days=60),
        last_attempt_status="succeeded",
        last_attempt_at=NOW - timedelta(days=checked_offset),
    )


def _permission(endpoint_id: int, *, documented: bool = False):
    return ContactPermissionEvidence(
        id=99,
        project_id=10,
        user_id=20,
        endpoint_id=endpoint_id,
        channel="email",
        permission_type="marketing",
        status="granted",
        source="signed_service_form" if documented else "consent_checkbox",
        evidence_ref="contract:123" if documented else "form:123",
        evidence_metadata={"legal_basis": "existing_customer_relationship"} if documented else {},
        captured_at=NOW - timedelta(days=1),
    )


def test_campaign_policy_never_allows_lane_relabel_or_weak_verification():
    with _ASSERT_RAISES(CampaignPolicyError):
        validate_policy({"purpose": "transactional"})
    with _ASSERT_RAISES(CampaignPolicyError):
        validate_policy({"verification_mode": "report_only"})
    with _ASSERT_RAISES(CampaignPolicyError):
        validate_policy({"permission_mode": "legacy"})
    with _ASSERT_RAISES(CampaignPolicyError):
        validate_policy({"permission_type": "transactional"})


def test_campaign_override_accepts_only_soft_pacing_rules():
    normalized = normalize_override_request({
        "rules": ["contact_caps", "channel_cooldown", "contact_caps"],
        "reason": "Urgent run approved with a documented business reason",
        "risk_acknowledged": True,
        "expires_in_hours": 24,
    })
    assert normalized["rules"] == ["channel_cooldown", "contact_caps"]
    with _ASSERT_RAISES(CampaignOverrideError):
        normalize_override_request({
            "rules": ["consent"],
            "reason": "Consent must remain an immutable campaign guard",
            "risk_acknowledged": True,
        })
    with _ASSERT_RAISES(CampaignOverrideError):
        normalize_override_request({
            "rules": ["channel_cooldown"],
            "reason": "Risk acknowledgement cannot be inferred",
            "risk_acknowledged": False,
        })


def test_subscribed_no_optout_requires_explicit_persistable_risk_acknowledgement():
    with _ASSERT_RAISES(CampaignPolicyError):
        validate_policy({"permission_mode": "subscribed_no_optout"})
    policy = validate_policy({
        "permission_mode": "subscribed_no_optout",
        "risk_acknowledgement": {
            "accepted": True,
            "reason": "Documented migration cohort approved by workspace admin",
        },
    })
    endpoint = _endpoint(1, "a" * 64, primary=True)
    verification = _verification(1, endpoint.value_hash)
    reason = CampaignEligibilityService._suppression_reason(
        user=_user(),
        endpoint=endpoint,
        endpoint_error=None,
        verification=verification,
        permission=None,
        policy=policy,
        now=NOW,
    )
    assert reason is None


def test_explicit_and_documented_permission_modes_require_their_own_evidence():
    endpoint = _endpoint(1, "a" * 64, primary=True)
    verification = _verification(1, endpoint.value_hash)
    base = dict(
        user=_user(), endpoint=endpoint, endpoint_error=None,
        verification=verification, now=NOW,
    )
    explicit = validate_policy({"permission_mode": "explicit_consent"})
    assert CampaignEligibilityService._suppression_reason(
        **base, permission=None, policy=explicit,
    ) == "missing_permission_evidence"
    assert CampaignEligibilityService._suppression_reason(
        **base, permission=_permission(1), policy=explicit,
    ) is None

    documented = validate_policy({"permission_mode": "documented_relationship"})
    assert CampaignEligibilityService._suppression_reason(
        **base, permission=_permission(1), policy=documented,
    ) == "missing_documented_relationship_evidence"
    assert CampaignEligibilityService._suppression_reason(
        **base, permission=_permission(1, documented=True), policy=documented,
    ) is None


def test_verification_must_match_exact_endpoint_and_last_attempt_must_succeed():
    endpoint = _endpoint(1, "a" * 64, primary=True)
    permission = _permission(1)
    policy = validate_policy({"permission_mode": "explicit_consent"})
    verification = _verification(1, endpoint.value_hash)
    verification.last_attempt_status = "failed"
    assert CampaignEligibilityService._suppression_reason(
        user=_user(), endpoint=endpoint, endpoint_error=None,
        verification=verification, permission=permission, policy=policy, now=NOW,
    ) == "email_verification_last_attempt_failed"

    verification.last_attempt_status = "succeeded"
    verification.endpoint_id = 2
    assert CampaignEligibilityService._suppression_reason(
        user=_user(), endpoint=endpoint, endpoint_error=None,
        verification=verification, permission=permission, policy=policy, now=NOW,
    ) == "verification_endpoint_mismatch"


def test_email_selection_rule_can_choose_a_verified_secondary_endpoint_exactly_once():
    primary = _endpoint(1, "a" * 64, primary=True, seen_offset=1)
    secondary = _endpoint(2, "b" * 64, primary=False, seen_offset=2)
    secondary_verification = _verification(2, secondary.value_hash)
    selected, verification, reason = CampaignEligibilityService._select_endpoint(
        [(primary, None), (secondary, secondary_verification)],
        "most_recently_verified",
        NOW,
    )
    assert reason is None
    assert selected.id == secondary.id
    assert verification.endpoint_id == secondary.id

    selected, _, _ = CampaignEligibilityService._select_endpoint(
        [(primary, None), (secondary, secondary_verification)],
        "primary_then_verified",
        NOW,
    )
    assert selected.id == primary.id


def test_primary_only_selection_fails_closed_when_multiple_endpoints_are_ambiguous():
    first = _endpoint(1, "a" * 64)
    second = _endpoint(2, "b" * 64)
    selected, verification, reason = CampaignEligibilityService._select_endpoint(
        [(first, None), (second, None)], "primary", NOW,
    )
    assert selected is None
    assert verification is None
    assert reason == "ambiguous_email_endpoint"


def test_month_end_and_biweekly_recurrence_are_calendar_based():
    january_31 = datetime(2026, 1, 31, 13, 0, 0)
    february = next_occurrence(
        anchor=january_31,
        after=january_31,
        config={"frequency": "monthly"},
        timezone_name="UTC",
    )
    assert february == datetime(2026, 2, 28, 13, 0, 0)

    biweekly = next_occurrence(
        anchor=datetime(2026, 8, 1, 12, 0, 0),
        after=datetime(2026, 8, 1, 12, 0, 0),
        config={"frequency": "biweekly"},
        timezone_name="UTC",
    )
    assert biweekly == datetime(2026, 8, 15, 12, 0, 0)


def test_profile_capacity_keeps_transactional_margin_and_warmup_limit():
    profile = ChannelDeliveryProfile(
        max_per_minute=10,
        max_per_hour=500,
        max_per_day=2000,
        timezone="UTC",
        config={"send_window": {"start": "09:00", "end": "17:00"}},
        warmup_config={"current_daily_limit": 1000},
    )
    assert _daily_capacity(profile, {"capacity_margin_percent": 10}) == 900


def test_smtp_senders_are_grouped_by_real_provider_without_exposing_hosts():
    assert DeliveryProfileService._smtp_provider("smtp.gmail.com") == "gmail"
    assert DeliveryProfileService._smtp_provider("email-smtp.us-east-1.amazonaws.com") == "amazon_ses"
    assert DeliveryProfileService._smtp_provider("smtp.office365.com") == "microsoft_smtp"
    assert DeliveryProfileService._smtp_provider("mail.example.test") == "smtp"


def test_smtp_disconnect_during_submission_is_unknown_and_never_a_safe_retry():
    class DisconnectingSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def login(self, *_args, **_kwargs):
            return None

        def sendmail(self, *_args, **_kwargs):
            raise smtplib.SMTPServerDisconnected("connection lost after DATA")

    original = smtplib.SMTP
    smtplib.SMTP = DisconnectingSMTP
    try:
        result = EmailSender(None)._send_via_smtp(
            SimpleNamespace(
                id=1,
                from_email="sender@example.test",
                from_name="Sender",
                smtp_password_enc="password",
                smtp_use_ssl=False,
                smtp_use_tls=False,
                smtp_server="smtp.example.test",
                smtp_port=25,
                smtp_username="sender",
            ),
            "recipient@example.test",
            "Subject",
            "Body",
            message_id="<campaign-stable@example.test>",
        )
    finally:
        smtplib.SMTP = original
    assert result["success"] is False
    assert result["error_code"] == "submission_unknown"
    assert result["provider_message_id"] == "<campaign-stable@example.test>"


def test_group_event_filter_compiles_to_correlated_set_based_exists():
    predicate = compile_condition(
        10,
        {"field": "event", "value": "feature.used", "within_days": 30},
        NOW,
    )
    statement = select(MessagingUser.id).where(
        MessagingUser.project_id == 10,
        predicate,
    )
    sql = str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert "EXISTS" in sql
    assert "messaging_events.project_id = 10" in sql
    assert "messaging_events.user_id = messaging_users.id" in sql


def test_group_date_filters_normalize_offsets_to_naive_utc():
    assert _coerce_datetime("2026-08-22T09:00:00-03:00") == datetime(
        2026, 8, 22, 12, 0, 0,
    )
