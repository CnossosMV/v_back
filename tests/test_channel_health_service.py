from app.models import Project, SendLog, User, Workspace
from app.services.channels.channel_health_service import ChannelHealthService


def _project(db) -> Project:
    owner = User(email="channel-health@example.test", name="Channel Health", is_active=True)
    db.add(owner)
    db.flush()
    workspace = Workspace(name="Channel Health", owner_id=owner.id, is_active=True)
    db.add(workspace)
    db.flush()
    project = Project(
        name="Channel Health",
        workspace_id=workspace.id,
        is_active=True,
        pii_salt="c" * 64,
    )
    db.add(project)
    db.flush()
    return project


def _log(project_id: int, status: str, **overrides) -> SendLog:
    values = {
        "project_id": project_id,
        "channel": "email",
        "recipient": "health@example.test",
        "content_type": "text",
        "source_type": "event_action",
        "status": status,
        "is_historical": False,
    }
    values.update(overrides)
    return SendLog(**values)


def test_project_health_distinguishes_attempts_sends_and_blocks(db_session):
    project = _project(db_session)
    db_session.add_all([
        _log(project.id, "sent"),
        _log(project.id, "delivered"),
        _log(
            project.id,
            "blocked",
            error_message="Outbound source contract blocked dispatch: event_action has no attention_policy",
        ),
        _log(project.id, "blocked", error_message="All channels blocked by missing consent"),
        _log(project.id, "blocked", is_historical=True, error_message="Imported history"),
    ])
    db_session.commit()

    result = ChannelHealthService(db_session).get_project_health(project.id, hours=24)

    assert result["total_attempts"] == 4
    assert result["total_sent"] == 2
    assert result["delivered_count"] == 1
    assert result["blocked_count"] == 2
    assert result["status_counts"] == {"blocked": 2, "delivered": 1, "sent": 1}
    assert {row["code"] for row in result["blocked_reasons"]} == {
        "missing_consent",
        "source_contract_missing_attention_policy",
    }
    assert result["channels"] == [{
        "channel": "email",
        "total": 4,
        "total_attempts": 4,
        "sent": 2,
        "blocked": 2,
        "pending": 0,
        "skipped": 0,
        "delivered": 1,
        "read": 0,
        "failed": 0,
        "delivery_rate": 50.0,
        "failure_rate": 0.0,
    }]
