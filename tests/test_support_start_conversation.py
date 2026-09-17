"""Regression coverage for agent-initiated support conversations."""

import pytest

from app import models
from app.models.messaging import ChannelType, MessagingTemplate, MessagingUser
from app.services.channels.base import OutboundContent, SendDecision, SendResult
from app.services.channels.email_adapter import EmailAdapter
from app.services.channels.send_service import SendService
from app.services.support_inbox_service import SupportInboxService


@pytest.fixture
def conversation_seed(db_session):
    agent = models.User(email="inbox-agent@example.test", name="Inbox Agent")
    db_session.add(agent)
    db_session.flush()

    workspace = models.Workspace(name="Inbox Workspace", owner_id=agent.id)
    db_session.add(workspace)
    db_session.flush()
    agent.workspace_id = workspace.id

    project = models.Project(name="Inbox Project", workspace_id=workspace.id)
    db_session.add(project)
    db_session.flush()
    contact = MessagingUser(
        project_id=project.id,
        external_id="contact-1",
        name="Test Contact",
        email="contact@example.test",
        phone="5511999999999",
        phone_e164="5511999999999",
        is_subscribed=True,
    )

    whatsapp = models.WhatsAppInstance(
        user_id=agent.id,
        workspace_id=workspace.id,
        project_id=project.id,
        provider_type="evolution_api",
        instance_name="inbox-regression-whatsapp",
    )
    email_instance = models.EmailInstance(
        user_id=agent.id,
        workspace_id=workspace.id,
        project_id=project.id,
        provider_type="smtp",
        instance_name="inbox-regression-email",
        from_email="instance@example.test",
        smtp_server="smtp.example.test",
        smtp_port=587,
        smtp_username="instance@example.test",
        smtp_password_enc="encrypted-test-value",
        is_active=True,
    )
    smtp_config = models.CustomerSMTPConfig(
        project_id=project.id,
        smtp_server="smtp.legacy.example.test",
        smtp_port=587,
        smtp_username="legacy@example.test",
        smtp_password="encrypted-test-value",
        from_email="legacy@example.test",
        from_name="Legacy Sender",
        is_active=True,
    )
    sms_template = MessagingTemplate(
        project_id=project.id,
        slug="inbox-regression-sms",
        name="Inbox regression SMS",
        channel_type=ChannelType.sms,
        body="SMS body",
        body_format="plain",
    )
    db_session.add_all([contact, whatsapp, email_instance, smtp_config, sms_template])
    db_session.commit()

    return {
        "agent": agent,
        "project": project,
        "contact": contact,
        "whatsapp": whatsapp,
        "email_instance": email_instance,
        "smtp_config": smtp_config,
        "sms_template": sms_template,
    }


@pytest.mark.parametrize("channel", ["email", "whatsapp", "sms"])
@pytest.mark.asyncio
async def test_start_conversation_builds_models_for_each_channel(
    db_session, conversation_seed, monkeypatch, channel
):
    """The bootstrap path must construct every ORM row before delivery."""

    async def fake_send(self, **kwargs):
        return SendDecision(
            success=True,
            send_log_id=123,
            channel_used=kwargs["channel"],
            status="sent",
        )

    monkeypatch.setattr(SendService, "send", fake_send)
    monkeypatch.setattr(SupportInboxService, "_publish", lambda *args, **kwargs: None)

    seed = conversation_seed
    service = SupportInboxService(db_session)
    payload = {
        "project_id": seed["project"].id,
        "agent_user_id": seed["agent"].id,
        "contact_id": seed["contact"].id,
        "channel": channel,
        "mode": "text",
        "body": f"Regression message for {channel}",
    }
    if channel == "email":
        payload["subject"] = "Regression subject"
    elif channel == "whatsapp":
        payload["whatsapp_instance_id"] = seed["whatsapp"].id
    else:
        payload["mode"] = "template"
        payload.pop("body")
        payload["template_id"] = seed["sms_template"].id

    result = await service.start_conversation(**payload)

    ticket = db_session.query(models.SupportTicket).filter_by(id=result["ticket_id"]).one()
    session = db_session.query(models.ChatSession).filter_by(id=result["session_id"]).one()
    message = db_session.query(models.ChatMessage).filter_by(session_id=session.id).one()
    routing = db_session.query(models.ContactRoutingState).filter_by(
        project_id=seed["project"].id,
        contact_identifier=("contact@example.test" if channel == "email" else "5511999999999"),
        channel=channel,
    ).one()

    assert result["status"] == "sent"
    assert ticket.assigned_to_user_id == seed["agent"].id
    assert ticket.channel == channel
    assert session.channel == channel
    assert message.sender_type == "human_agent"
    assert message.sent_by_user_id == seed["agent"].id
    assert routing.handler_type == "human"
    assert routing.handler_id == ticket.id
    assert routing.handler_priority == 100
    assert routing.session_id == session.id
    assert routing.expires_at is None
    routing_id = routing.id

    service.update_ticket(ticket.id, status="resolved")
    assert db_session.query(models.ContactRoutingState).filter_by(id=routing_id).count() == 0

@pytest.mark.asyncio
async def test_start_conversation_uses_selected_smtp_sender(
    db_session, conversation_seed, monkeypatch
):
    captured = {}

    async def fake_send(self, **kwargs):
        captured.update(kwargs)
        return SendDecision(
            success=True,
            send_log_id=456,
            channel_used="email",
            status="sent",
        )

    monkeypatch.setattr(SendService, "send", fake_send)
    monkeypatch.setattr(SupportInboxService, "_publish", lambda *args, **kwargs: None)

    seed = conversation_seed
    service = SupportInboxService(db_session)
    result = await service.start_conversation(
        project_id=seed["project"].id,
        agent_user_id=seed["agent"].id,
        contact_id=seed["contact"].id,
        channel="email",
        mode="text",
        body="Selected sender regression",
        subject="Selected sender",
        smtp_config_id=seed["smtp_config"].id,
        from_email="sales@example.test",
        from_name="Sales Team",
        reply_to="replies@example.test",
    )

    assert result["status"] == "sent"
    assert captured["instance_config"] == {
        "smtp_config_id": seed["smtp_config"].id,
        "from_email": "sales@example.test",
        "from_name": "Sales Team",
        "reply_to": "replies@example.test",
    }
    assert captured["content"].metadata == {
        "from_email": "sales@example.test",
        "from_name": "Sales Team",
        "reply_to": "replies@example.test",
    }


@pytest.mark.asyncio
async def test_selected_legacy_smtp_bypasses_email_instance(
    db_session, conversation_seed, monkeypatch
):
    captured = {}

    async def fail_instance_send(*args, **kwargs):
        raise AssertionError("EmailInstance must not be used when SMTP config is selected")

    def fake_smtp_send(self, db, project_id, recipient, content, cfg):
        captured.update(cfg)
        return SendResult(success=True, provider_message_id="smtp-selected")

    monkeypatch.setattr(EmailAdapter, "_send_via_instance", fail_instance_send)
    monkeypatch.setattr(EmailAdapter, "_send_via_smtp", fake_smtp_send)

    seed = conversation_seed
    result = await EmailAdapter().send(
        db_session,
        seed["project"].id,
        seed["contact"].email,
        OutboundContent(content_type="text", text="Hello"),
        {"smtp_config_id": seed["smtp_config"].id},
    )

    assert result.success is True
    assert result.provider_message_id == "smtp-selected"
    assert captured["smtp_config_id"] == seed["smtp_config"].id


@pytest.mark.asyncio
async def test_start_conversation_rejects_email_instance_from_another_project(
    db_session, conversation_seed
):
    seed = conversation_seed
    other_project = models.Project(
        name="Other Inbox Project",
        workspace_id=seed["project"].workspace_id,
    )
    db_session.add(other_project)
    db_session.flush()
    other_instance = models.EmailInstance(
        user_id=seed["agent"].id,
        workspace_id=seed["project"].workspace_id,
        project_id=other_project.id,
        provider_type="smtp",
        instance_name="other-project-email",
        from_email="other@example.test",
        smtp_server="smtp.example.test",
        smtp_port=587,
        smtp_username="other@example.test",
        smtp_password_enc="encrypted-test-value",
        is_active=True,
    )
    db_session.add(other_instance)
    db_session.commit()

    service = SupportInboxService(db_session)
    with pytest.raises(ValueError, match="not available for this project"):
        await service.start_conversation(
            project_id=seed["project"].id,
            agent_user_id=seed["agent"].id,
            contact_id=seed["contact"].id,
            channel="email",
            mode="text",
            body="Must not send",
            subject="Wrong tenant",
            email_instance_id=other_instance.id,
        )
