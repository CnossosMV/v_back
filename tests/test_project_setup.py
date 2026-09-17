import hashlib
import io
import zipfile
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app import models
from app.main import app
from app.services.agent_skill_service import (
    SKILL_NAME,
    SKILL_VERSION,
    build_skill_zip,
    skill_manifest,
)
from app.services.project_foundation import normalize_foundation_update
from app.services.project_market_service import ProjectMarketError, ProjectMarketService
from app.services.project_setup_service import (
    DEFAULT_PHASES,
    ProjectSetupService,
    normalize_decision_input,
    normalize_plan_data,
)


def _project(**overrides):
    values = {
        "id": 7,
        "name": "Tenant",
        "workspace_id": 3,
        "is_active": True,
        "default_locale": "pt-BR",
        "supported_locales": ["pt-BR"],
        "default_timezone": "America/Sao_Paulo",
        "goal_event": None,
        "market_config": None,
    }
    values.update(overrides)
    return models.Project(**values)


def test_foundation_separates_market_from_locale_and_returns_exact_diff():
    report = normalize_foundation_update(
        _project(),
        {
            "supported_locales": ["pt-BR", "en-US"],
            "goal_event": "subscription.paid",
            "market_config": {
                "default_market_key": "br",
                "markets": [
                    {
                        "key": "br",
                        "country_code": "BR",
                        "region_codes": ["SP"],
                        "timezone": "America/Sao_Paulo",
                        "locales": ["pt-BR"],
                        "calendar_tags": ["retail"],
                    }
                ],
            },
        },
    )

    assert report["after"]["goal_event"] == "subscription.paid"
    assert report["after"]["market_config"]["default_market_key"] == "br"
    assert report["after"]["supported_locales"] == ["pt-BR", "en-US"]
    assert set(report["changes"]) == {"supported_locales", "goal_event", "market_config"}


def test_foundation_rejects_unknown_timezone_and_market_locale():
    with pytest.raises(ValueError, match="IANA timezone"):
        normalize_foundation_update(_project(), {"default_timezone": "Sao Paulo"})

    with pytest.raises(ValueError, match="unsupported project locales"):
        normalize_foundation_update(
            _project(),
            {
                "market_config": {
                    "default_market_key": "us",
                    "markets": [
                        {
                            "key": "us",
                            "country_code": "US",
                            "timezone": "America/New_York",
                            "locales": ["en-US"],
                        }
                    ],
                }
            },
        )


def test_foundation_market_count_is_not_limited_by_page_size():
    markets = [
        {
            "key": f"m{index}",
            "country_code": "BR",
            "timezone": "America/Sao_Paulo",
            "locales": ["pt-BR"],
        }
        for index in range(201)
    ]
    report = normalize_foundation_update(
        _project(),
        {
            "market_config": {
                "default_market_key": "m0",
                "markets": markets,
            }
        },
    )

    assert len(report["after"]["market_config"]["markets"]) == 201


def test_first_class_markets_paginate_archive_and_sync_legacy_projection(db_session):
    user = models.User(
        email="market-owner@example.com",
        name="Market Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.flush()
    workspace = models.Workspace(name="Market workspace", owner_id=user.id, is_active=True)
    db_session.add(workspace)
    db_session.flush()
    user.workspace_id = workspace.id
    project = models.Project(
        name="Multi-market",
        workspace_id=workspace.id,
        default_locale="pt-BR",
        supported_locales=["pt-BR", "en-US"],
        default_timezone="America/Sao_Paulo",
    )
    db_session.add(project)
    db_session.flush()

    service = ProjectMarketService(db_session)
    br = service.create(project.id, {
        "key": "br",
        "country_code": "BR",
        "timezone": "America/Sao_Paulo",
        "locales": ["pt-BR"],
        "calendar_tags": ["br"],
    })
    us_definition = {
        "key": "us",
        "country_code": "US",
        "timezone": "America/New_York",
        "locales": ["en-US"],
        "calendar_tags": ["us"],
    }
    # MCP confirmation passes the normalized preview back to the service.
    us = service.create(project.id, service.preview_create(project.id, us_definition))
    db_session.flush()

    assert br.is_default is True
    assert us.is_default is False
    assert project.market_config["default_market_key"] == "br"
    first = service.list_page(project.id, limit=1)
    second = service.list_page(project.id, limit=1, after_key=first["next_cursor"])
    assert [item["key"] for item in first["markets"]] == ["br"]
    assert first["has_more"] is True
    assert [item["key"] for item in second["markets"]] == ["us"]

    service.set_default(project.id, "us")
    with pytest.raises(ProjectMarketError, match="another default"):
        service.set_status(project.id, "us", "archived")
    service.set_status(project.id, "br", "archived")
    assert project.market_config["default_market_key"] == "us"
    assert [item["key"] for item in project.market_config["markets"]] == ["us"]
    service.set_status(project.id, "br", "active")
    assert [item["key"] for item in project.market_config["markets"]] == ["br", "us"]


def test_default_setup_plan_has_dependency_and_independent_execution_gate():
    data = normalize_plan_data(None, ["No historical source"], [])

    assert data["phases"][0]["key"] == "discover"
    assert data["phases"][1]["dependencies"] == ["discover"]
    assert "no setup-plan operation authorizes a send" in data["execution_rule"].lower()


def test_setup_plan_rejects_cyclic_phase_dependencies():
    phases = [dict(DEFAULT_PHASES[0]), dict(DEFAULT_PHASES[1])]
    phases[0]["dependencies"] = ["foundation"]
    phases[1]["dependencies"] = ["discover"]

    with pytest.raises(ValueError, match="cycle"):
        normalize_plan_data(phases, [], [])


def test_only_tenant_decided_values_can_be_accepted():
    with pytest.raises(ValueError, match="tenant_decided"):
        normalize_decision_input(
            "market.default", "recommended", "accepted", "br", "Recommended default", []
        )

    accepted = normalize_decision_input(
        "market.default", "tenant_decided", "accepted", "br", "Tenant chose Brazil", []
    )
    assert accepted["status"] == "accepted"


def test_project_assessment_marks_market_and_goal_as_tenant_decisions():
    output = ProjectSetupService.assessment(_project(), {"templates": 0})

    assert output["foundation_ready"] is False
    assert output["missing_tenant_decisions"] == ["project.goal_event", "market.default"]
    assert output["evidence_labels"]["recommendation"] == "recommended"
    assert output["external_sends"] == 0


def test_persistent_decision_changes_fingerprint_and_stales_phase_approval(db_session):
    user = models.User(
        email="setup-owner@example.com",
        name="Owner",
        role="user",
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.flush()
    workspace = models.Workspace(name="Setup workspace", owner_id=user.id, is_active=True)
    db_session.add(workspace)
    db_session.flush()
    user.workspace_id = workspace.id
    project = models.Project(
        name="Setup project",
        workspace_id=workspace.id,
        is_active=True,
        default_locale="pt-BR",
        supported_locales=["pt-BR"],
        default_timezone="America/Sao_Paulo",
    )
    db_session.add(project)
    db_session.commit()

    service = ProjectSetupService(db_session)
    plan = service.create_plan(
        project,
        "Initial setup",
        "Deliver the first observable value slice safely.",
        normalize_plan_data(None, [], []),
        service.assessment(project),
        user.id,
    )
    service.record_decision(
        plan,
        "business.first_value_outcome",
        "tenant_decided",
        "accepted",
        "subscription.paid",
        "The tenant chose paid subscription as the first outcome.",
        [],
        user.id,
    )
    approved_fingerprint = service.current_fingerprint(plan)
    approval = service.approve_phase(plan, "discover", "Discovery is correct.", user.id)
    assert approval.plan_fingerprint == approved_fingerprint
    assert service.phase_gate(plan, "discover")["current_approval_id"] == approval.id

    service.record_decision(
        plan,
        "operating.copy_autonomy",
        "recommended",
        "proposed",
        "agent_draft",
        "Start with reviewable agent drafts.",
        [],
        user.id,
    )
    changed_gate = service.phase_gate(plan, "discover")
    assert changed_gate["current_plan_fingerprint"] != approved_fingerprint
    assert changed_gate["current_approval_id"] is None
    assert approval.id in changed_gate["stale_approval_ids"]
    assert changed_gate["execution_authorized"] is False

    with pytest.raises(ValueError, match="fingerprint changed"):
        service.record_decision(
            plan,
            "market.default",
            "tenant_decided",
            "accepted",
            "br",
            "This write was based on the stale plan.",
            [],
            user.id,
            expected_fingerprint=approved_fingerprint,
        )


def test_public_skill_zip_is_deterministic_versioned_and_self_describing():
    first = build_skill_zip()
    second = build_skill_zip()
    manifest = skill_manifest()

    assert first == second
    assert manifest["version"] == SKILL_VERSION
    assert manifest["zip_sha256"] == hashlib.sha256(first).hexdigest()
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        names = set(archive.namelist())
        assert f"{SKILL_NAME}/SKILL.md" in names
        assert f"{SKILL_NAME}/manifest.json" in names
        assert f"{SKILL_NAME}/references/project-setup-runbook.md" in names
        assert f"{SKILL_NAME}/references/project-markets.md" in names


def test_public_skill_routes_support_manifest_download_and_etag():
    client = TestClient(app)
    manifest_response = client.get(f"/agent-skills/{SKILL_NAME}/manifest.json")
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["zip_sha256"]
    assert manifest["external_sends"] == 0

    not_modified = client.get(
        f"/agent-skills/{SKILL_NAME}/manifest.json",
        headers={"If-None-Match": manifest_response.headers["etag"]},
    )
    assert not_modified.status_code == 304

    download_path = urlparse(manifest["download_url"]).path
    download = client.get(download_path)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert hashlib.sha256(download.content).hexdigest() == manifest["zip_sha256"]
