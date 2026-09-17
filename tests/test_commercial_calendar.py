from datetime import datetime

from app.schemas.campaigns import CampaignCreate
from app.schemas.commercial_calendar import OpportunityPreviewInput
from app.services.campaigns.commercial_calendar import CommercialCalendarService


def test_calendar_explains_thursday_month_boundary_overlap():
    request = OpportunityPreviewInput(**{
        "horizon_start": "2026-10-26T00:00:00Z",
        "horizon_end": "2026-11-07T00:00:00Z",
        "include_persisted": False,
        "rules": [
            {
                "rule_key": "weekend_offer",
                "name": "Weekend offer",
                "rule_type": "weekly",
                "timezone": "UTC",
                "country_code": "BR",
                "priority": 50,
                "priority_source": "tenant_policy",
                "priority_reason": "Ordinary weekend offer",
                "config": {"weekdays": [4], "start_time": "10:00", "duration_hours": 72},
            },
            {
                "rule_key": "month_boundary",
                "name": "Month boundary",
                "rule_type": "month_boundary",
                "timezone": "UTC",
                "country_code": "BR",
                "priority": 80,
                "priority_source": "tenant_policy",
                "priority_reason": "Month boundary outranks an ordinary weekend",
                "config": {"days_before": 2, "days_after": 5, "start_time": "09:00", "end_time": "23:00"},
            },
        ],
    })

    result = CommercialCalendarService(None).preview(1, request)

    overlap = next(
        item for item in result["overlaps"]
        if any(key.startswith("weekend_offer:") for key in item["opportunity_keys"])
        and any(key.startswith("month_boundary:") for key in item["opportunity_keys"])
    )
    assert overlap["declared_winner"].startswith("month_boundary:")
    assert overlap["declared_priority"] == 80
    assert overlap["knowledge_source"] == "tenant_policy"
    assert overlap["resolution_required"] is False
    assert overlap["combined_candidate_recommended"] is True
    assert result["external_sends"] == 0


def test_campaign_accepts_structured_opportunity_context():
    payload = CampaignCreate(**{
        "name": "Expired trial month boundary",
        "selection_config": {"rule_config": {"filters": []}},
        "purpose_key": "trial.conversion",
        "opportunity_config": {
            "opportunity_type": "month_boundary",
            "priority": 80,
            "priority_source": "tenant_policy",
            "priority_reason": "Month boundary is the primary conversion moment",
            "decision_lead_hours": 168,
            "attention_window_hours": 120,
            "combine_with": ["weekend_offer"],
        },
        "actions": [{
            "channel": "email",
            "variants": [{"locale": "pt-BR", "subject": "Oferta", "body": "Volte para criar."}],
        }],
    })

    assert payload.opportunity_config is not None
    assert payload.opportunity_config.priority == 80
    assert payload.opportunity_config.decision_lead_hours == 168


def test_calendar_does_not_invent_winner_for_unresolved_policy():
    request = OpportunityPreviewInput(**{
        "horizon_start": "2026-10-29T00:00:00Z",
        "horizon_end": "2026-11-02T00:00:00Z",
        "include_persisted": False,
        "rules": [
            {
                "rule_key": "weekend_offer",
                "name": "Weekend offer",
                "rule_type": "weekly",
                "timezone": "UTC",
                "priority": 50,
                "config": {"weekdays": [4], "start_time": "10:00", "duration_hours": 72},
            },
            {
                "rule_key": "month_boundary",
                "name": "Month boundary",
                "rule_type": "month_boundary",
                "timezone": "UTC",
                "priority": 50,
                "config": {"days_before": 2, "days_after": 2},
            },
        ],
    })

    result = CommercialCalendarService(None).preview(1, request)

    overlap = result["overlaps"][0]
    assert overlap["declared_winner"] is None
    assert overlap["knowledge_source"] == "tie_requires_decision"
    assert overlap["resolution_required"] is True
