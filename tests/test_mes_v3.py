from datetime import datetime, timedelta

import pytest

from app.models import (
    MessageEffectivenessScore,
    Project,
    SendLog,
    User,
    Workspace,
)
from app.models.messaging import MessagingEvent, MessagingUser
from app.services.messaging.event_sources import BACKEND, FRONTEND, SYSTEM
from app.services.scoring.mes_engine import ALGORITHM_VERSION, MESEngine
from app.services.scoring.mes_scheduler import MESSchedulerWorker


def _postgres_only(db):
    if db.bind.dialect.name != "postgresql":
        pytest.skip("MES JSONB integration tests require PostgreSQL")


def _seed_project(db):
    owner = User(email="mes-v3@example.com", name="MES v3 Owner")
    db.add(owner)
    db.flush()

    workspace = Workspace(name="MES v3", owner_id=owner.id)
    db.add(workspace)
    db.flush()
    owner.workspace_id = workspace.id

    project = Project(name="MES v3", workspace_id=workspace.id)
    db.add(project)
    db.flush()

    contact = MessagingUser(
        project_id=project.id,
        external_id="contact-1",
        email="contact@example.com",
    )
    db.add(contact)
    db.flush()
    return project, contact


def _send(db, project, contact, sent_at, **overrides):
    values = {
        "project_id": project.id,
        "user_id": contact.id if contact else None,
        "channel": "email",
        "recipient": contact.email if contact else "anonymous@example.com",
        "content_type": "html",
        "source_type": "support",
        "status": "sent",
        "queued_at": sent_at - timedelta(seconds=1),
        "sent_at": sent_at,
    }
    values.update(overrides)
    send = SendLog(**values)
    db.add(send)
    db.flush()
    return send


def _event(db, project, created_at, source, **overrides):
    values = {
        "project_id": project.id,
        "event_name": "evento_customizado",
        "source": source,
        "properties": {},
        "created_at": created_at,
    }
    values.update(overrides)
    event = MessagingEvent(**values)
    db.add(event)
    db.flush()
    return event


def test_mes_v3_uses_frontend_identity_or_direct_attribution_only(db_session):
    _postgres_only(db_session)
    project, contact = _seed_project(db_session)
    sent_at = datetime.utcnow() - timedelta(hours=1)
    send = _send(db_session, project, contact, sent_at)
    other_send = _send(db_session, project, contact, sent_at + timedelta(minutes=2))

    _event(
        db_session,
        project,
        sent_at + timedelta(minutes=5),
        FRONTEND,
        user_id=contact.id,
    )
    _event(
        db_session,
        project,
        sent_at + timedelta(minutes=6),
        BACKEND,
        user_id=contact.id,
    )
    _event(
        db_session,
        project,
        sent_at + timedelta(minutes=7),
        SYSTEM,
        user_id=contact.id,
    )
    _event(
        db_session,
        project,
        sent_at + timedelta(minutes=8),
        FRONTEND,
        anonymous_id="anon-direct",
        attribution={"send_log_id": send.id},
    )
    _event(
        db_session,
        project,
        sent_at + timedelta(minutes=9),
        FRONTEND,
        anonymous_id="anon-unlinked",
    )
    _event(
        db_session,
        project,
        sent_at + timedelta(minutes=10),
        FRONTEND,
        user_id=contact.id,
        attribution={"send_log_id": other_send.id},
    )

    score, signals = MESEngine(db_session).compute_reengagement_score(send, None)

    assert score > 0
    assert signals["eligible_event_sources"] == [FRONTEND]
    assert signals["events_found"] == 2
    assert signals["hard_linked_events"] == 1
    assert signals["excluded_other_send_events"] == 1


def test_mes_v3_baseline_and_pre_session_ignore_backend(db_session):
    _postgres_only(db_session)
    project, contact = _seed_project(db_session)
    sent_at = datetime.utcnow() - timedelta(hours=1)
    send = _send(db_session, project, contact, sent_at)

    _event(
        db_session,
        project,
        sent_at - timedelta(minutes=5),
        BACKEND,
        user_id=contact.id,
    )
    _event(
        db_session,
        project,
        sent_at - timedelta(days=1),
        BACKEND,
        user_id=contact.id,
    )
    _event(
        db_session,
        project,
        sent_at + timedelta(minutes=5),
        FRONTEND,
        user_id=contact.id,
    )

    score, signals = MESEngine(db_session).compute_reengagement_score(send, None)

    assert score > 0
    assert signals["pre_session_detected"] is False
    assert signals["pre_session_event_count"] == 0
    assert signals["baseline_events_7d"] == 0


def test_email_click_affects_reach_not_reengagement(db_session):
    _postgres_only(db_session)
    project, contact = _seed_project(db_session)
    sent_at = datetime.utcnow() - timedelta(hours=1)
    send = _send(
        db_session,
        project,
        contact,
        sent_at,
        click_count=1,
        first_click_at=sent_at + timedelta(minutes=3),
    )

    engine = MESEngine(db_session)
    reach, _ = engine.compute_reach_score(send)
    reengagement, signals = engine.compute_reengagement_score(send, None)

    assert reach > 0
    assert reengagement == 0
    assert signals["events_found"] == 0


def test_direct_anonymous_event_marks_its_message_stale(db_session):
    _postgres_only(db_session)
    project, contact = _seed_project(db_session)
    sent_at = datetime.utcnow() - timedelta(hours=1)
    send = _send(db_session, project, contact, sent_at)
    mes = MessageEffectivenessScore(
        project_id=project.id,
        send_log_id=send.id,
        user_id=contact.id,
        channel="email",
        source_type="support",
        algorithm_version=2,
        stale=False,
        settled=True,
    )
    db_session.add(mes)
    db_session.flush()
    event = _event(
        db_session,
        project,
        sent_at + timedelta(minutes=4),
        FRONTEND,
        event_name="page_view",
        anonymous_id="anon-stale",
        attribution={"send_log_id": send.id},
    )

    MESEngine(db_session).mark_stale_for_event(event)
    db_session.expire_all()

    refreshed = db_session.query(MessageEffectivenessScore).filter_by(id=mes.id).one()
    assert refreshed.stale is True


def test_old_settled_scores_are_drained_in_batches_to_v3(db_session):
    _postgres_only(db_session)
    project, contact = _seed_project(db_session)
    sent_at = datetime.utcnow() - timedelta(days=2)

    for index in range(5):
        send = _send(
            db_session,
            project,
            contact,
            sent_at + timedelta(minutes=index),
            recipient=f"contact+{index}@example.com",
        )
        db_session.add(MessageEffectivenessScore(
            project_id=project.id,
            send_log_id=send.id,
            user_id=contact.id,
            channel="email",
            source_type="support",
            algorithm_version=2,
            stale=False,
            settled=True,
        ))
    db_session.commit()

    engine = MESEngine(db_session)
    for _ in range(3):
        assert engine.batch_compute(project.id, limit=2) > 0

    assert db_session.query(MessageEffectivenessScore).filter(
        MessageEffectivenessScore.algorithm_version != ALGORITHM_VERSION,
    ).count() == 0


def test_scheduler_discovers_settled_old_versions(db_session):
    _postgres_only(db_session)
    project, contact = _seed_project(db_session)
    sent_at = datetime.utcnow() - timedelta(days=2)
    send = _send(db_session, project, contact, sent_at)
    mes = MessageEffectivenessScore(
        project_id=project.id,
        send_log_id=send.id,
        user_id=contact.id,
        channel="email",
        source_type="support",
        algorithm_version=2,
        stale=False,
        settled=True,
    )
    db_session.add(mes)
    db_session.commit()

    assert MESSchedulerWorker()._process_cycle(db_session) == 1
    db_session.refresh(mes)
    assert mes.algorithm_version == ALGORITHM_VERSION
    assert mes.stale is False