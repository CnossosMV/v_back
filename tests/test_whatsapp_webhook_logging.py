import logging

import pytest

from app.routers.whatsapp import whatsapp_webhook


class _NoInstanceQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None


class _NoInstanceDb:
    def query(self, *args, **kwargs):
        return _NoInstanceQuery()


@pytest.mark.asyncio
async def test_evolution_webhook_does_not_log_secrets_or_qr_payload(caplog):
    payload = {
        "event": "qrcode.updated",
        "apikey": "provider-secret-must-not-be-logged",
        "data": {"qrcode": {"base64": "private-qr-image-must-not-be-logged"}},
    }

    with caplog.at_level(logging.INFO, logger="app.routers.whatsapp"):
        result = await whatsapp_webhook("unknown-instance", payload, _NoInstanceDb())

    assert result == {"status": "ignored", "reason": "unknown_instance"}
    assert "qrcode.updated" in caplog.text
    assert "provider-secret-must-not-be-logged" not in caplog.text
    assert "private-qr-image-must-not-be-logged" not in caplog.text
