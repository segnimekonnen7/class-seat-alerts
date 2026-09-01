"""
Notification tests.

The property worth testing on both channels is the same: does a failure get
classified as retryable or permanent correctly? Getting that backwards means
either retrying a blocked bot forever or giving up on a five-second outage.
"""

from __future__ import annotations

import smtplib

import httpx
import pytest

from app.models import Channel
from app.notifications import get_notifier
from app.notifications.base import (
    Message,
    PermanentDeliveryError,
    TransientDeliveryError,
    build_opening_message,
)
from app.notifications.email import EmailNotifier
from app.notifications.telegram import TelegramNotifier

MESSAGE = Message(subject="CS 320 is open", body="CS 320 opened. CRN 10432.")


def telegram_returning(status_code: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"ok": status_code == 200}, request=request)

    return httpx.MockTransport(handler)


# --- The message itself ----------------------------------------------------


def test_the_alert_leads_with_the_crn():
    """
    The CRN is what a student types into the registration form. The whole value
    of this service is the seconds between reading the alert and submitting;
    making someone go look the CRN up spends them.
    """
    message = build_opening_message(
        course_code="CS 320",
        title="Software Engineering",
        crn="005217",
        term="20273",
        seats_open=3,
        seats_total=30,
    )
    assert "005217" in message.body
    assert "CS 320" in message.subject


def test_the_alert_reports_the_seat_count():
    message = build_opening_message(
        course_code="CS 320",
        title="Software Engineering",
        crn="005217",
        term="20273",
        seats_open=3,
        seats_total=30,
    )
    assert "3 of 30" in message.body


def test_the_alert_handles_an_unknown_total():
    message = build_opening_message(
        course_code="CS 320",
        title="SE",
        crn="005217",
        term="20273",
        seats_open=2,
        seats_total=0,
    )
    assert "2 seats" in message.body


# --- Telegram --------------------------------------------------------------


def test_telegram_send_succeeds_on_200():
    TelegramNotifier(transport=telegram_returning(200)).send("123", MESSAGE)


def test_telegram_posts_the_body_to_the_chat():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True}, request=request)

    TelegramNotifier(transport=httpx.MockTransport(handler)).send("123", MESSAGE)

    assert captured["chat_id"] == "123"
    assert captured["text"] == MESSAGE.body


def test_a_blocked_bot_is_permanent():
    """403 means the user blocked us. Retrying is pointless forever."""
    with pytest.raises(PermanentDeliveryError):
        TelegramNotifier(transport=telegram_returning(403)).send("123", MESSAGE)


def test_a_bad_chat_id_is_permanent():
    with pytest.raises(PermanentDeliveryError):
        TelegramNotifier(transport=telegram_returning(400)).send("nonsense", MESSAGE)


def test_a_telegram_rate_limit_is_transient():
    with pytest.raises(TransientDeliveryError):
        TelegramNotifier(transport=telegram_returning(429)).send("123", MESSAGE)


def test_a_telegram_server_error_is_transient():
    with pytest.raises(TransientDeliveryError):
        TelegramNotifier(transport=telegram_returning(500)).send("123", MESSAGE)


def test_a_telegram_network_error_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(TransientDeliveryError):
        TelegramNotifier(transport=httpx.MockTransport(handler)).send("123", MESSAGE)


# --- Email -----------------------------------------------------------------


class FakeSMTP:
    """Records the calls the notifier makes, and can be told to fail."""

    def __init__(self, host: str, port: int, raises: Exception | None = None) -> None:
        self.host = host
        self.port = port
        self.raises = raises
        self.calls: list[str] = []
        self.sent: list[object] = []

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append("login")

    def send_message(self, message: object) -> None:
        if self.raises is not None:
            raise self.raises
        self.calls.append("send_message")
        self.sent.append(message)

    def quit(self) -> None:
        self.calls.append("quit")


def test_email_send_succeeds():
    fake = FakeSMTP("smtp.test", 587)
    EmailNotifier(smtp_factory=lambda h, p: fake).send("a@b.com", MESSAGE)

    assert "send_message" in fake.calls


def test_email_starts_tls_before_sending():
    """
    Order matters: logging in before STARTTLS puts the password on the wire in
    the clear. This asserts the sequence, not just that both happened.
    """
    fake = FakeSMTP("smtp.test", 587)
    EmailNotifier(smtp_factory=lambda h, p: fake).send("a@b.com", MESSAGE)

    assert fake.calls.index("starttls") < fake.calls.index("send_message")


def test_the_connection_is_closed_even_when_sending_fails():
    fake = FakeSMTP("smtp.test", 587, raises=smtplib.SMTPServerDisconnected("gone"))
    with pytest.raises(TransientDeliveryError):
        EmailNotifier(smtp_factory=lambda h, p: fake).send("a@b.com", MESSAGE)

    assert "quit" in fake.calls


def test_a_refused_recipient_is_permanent():
    """The address does not exist. No number of retries changes that."""
    fake = FakeSMTP("smtp.test", 587, raises=smtplib.SMTPRecipientsRefused({}))
    with pytest.raises(PermanentDeliveryError):
        EmailNotifier(smtp_factory=lambda h, p: fake).send("nope@b.com", MESSAGE)


def test_bad_smtp_credentials_are_permanent():
    """Retrying a bad password just locks the account out faster."""
    fake = FakeSMTP("smtp.test", 587, raises=smtplib.SMTPAuthenticationError(535, b"nope"))
    with pytest.raises(PermanentDeliveryError):
        EmailNotifier(smtp_factory=lambda h, p: fake).send("a@b.com", MESSAGE)


def test_an_smtp_outage_is_transient():
    fake = FakeSMTP("smtp.test", 587, raises=smtplib.SMTPConnectError(421, b"busy"))
    with pytest.raises(TransientDeliveryError):
        EmailNotifier(smtp_factory=lambda h, p: fake).send("a@b.com", MESSAGE)


def test_a_socket_error_is_transient():
    fake = FakeSMTP("smtp.test", 587, raises=OSError("connection reset"))
    with pytest.raises(TransientDeliveryError):
        EmailNotifier(smtp_factory=lambda h, p: fake).send("a@b.com", MESSAGE)


# --- Registry --------------------------------------------------------------


def test_the_registry_returns_the_right_channel():
    assert isinstance(get_notifier(Channel.telegram), TelegramNotifier)
    assert isinstance(get_notifier(Channel.email), EmailNotifier)
