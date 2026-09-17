import sys
from types import SimpleNamespace

import pytest

from app.services.push_notification_service import PushNotificationService


class FakeDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class FakeWebPushException(Exception):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def _install_pywebpush(monkeypatch, exception):
    def fake_webpush(**kwargs):
        raise exception

    monkeypatch.setitem(
        sys.modules,
        "pywebpush",
        SimpleNamespace(webpush=fake_webpush, WebPushException=FakeWebPushException),
    )


def _subscription():
    return SimpleNamespace(
        id=123,
        endpoint="https://push.example/sub",
        p256dh_key="p256dh",
        auth_key="auth",
        is_active=True,
        last_used_at=None,
    )


@pytest.mark.parametrize(
    "exception",
    [
        FakeWebPushException(
            "WebPushException: Push failed: 410 Gone",
            response=SimpleNamespace(status_code=410),
        ),
        FakeWebPushException(
            "WebPushException: Push failed: 404 Not Found",
            response=SimpleNamespace(status_code=404),
        ),
        FakeWebPushException("WebPushException: Push failed: 410 Gone"),
    ],
)
def test_permanent_webpush_failures_deactivate_subscription(monkeypatch, exception):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "fake-private-key")
    _install_pywebpush(monkeypatch, exception)
    sub = _subscription()
    db = FakeDb()

    sent = PushNotificationService(db)._send_to_subs([sub], "Title", "Body")

    assert sent == 0
    assert sub.is_active is False
    assert db.commits == 1


def test_transient_webpush_failure_keeps_subscription_active(monkeypatch):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "fake-private-key")
    _install_pywebpush(
        monkeypatch,
        FakeWebPushException(
            "WebPushException: Push failed: 500 Internal Server Error",
            response=SimpleNamespace(status_code=500),
        ),
    )
    sub = _subscription()
    db = FakeDb()

    sent = PushNotificationService(db)._send_to_subs([sub], "Title", "Body")

    assert sent == 0
    assert sub.is_active is True
    assert db.commits == 1
