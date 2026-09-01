"""
Email delivery over SMTP.

Slower than Telegram and kept as the fallback for anyone who does not want a
bot. The interesting part is the same as Telegram's: deciding which SMTP
failures are worth retrying.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import get_settings
from app.notifications.base import (
    Message,
    PermanentDeliveryError,
    TransientDeliveryError,
)


class EmailNotifier:
    name = "email"

    def __init__(self, smtp_factory: object | None = None) -> None:
        """
        `smtp_factory` is injected by tests so the send path -- headers, TLS
        handshake ordering, login, error mapping -- runs for real against a
        fake server object instead of being mocked away entirely.
        """
        self.settings = get_settings()
        self._smtp_factory = smtp_factory

    def _connect(self) -> smtplib.SMTP:
        if self._smtp_factory is not None:
            return self._smtp_factory(self.settings.smtp_host, self.settings.smtp_port)  # type: ignore[operator]
        return smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15)

    def send(self, destination: str, message: Message) -> None:
        if not self.settings.smtp_host:
            raise PermanentDeliveryError("SMTP_HOST is not configured")

        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = self.settings.smtp_from
        email["To"] = destination
        email.set_content(message.body)

        try:
            smtp = self._connect()
            try:
                if self.settings.smtp_use_tls:
                    smtp.starttls()
                if self.settings.smtp_username:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(email)
            finally:
                smtp.quit()
        except smtplib.SMTPRecipientsRefused as exc:
            # The address does not exist. No number of retries will change that.
            raise PermanentDeliveryError(f"recipient refused: {destination}") from exc
        except smtplib.SMTPAuthenticationError as exc:
            # Bad credentials. Retrying just locks the account out faster.
            raise PermanentDeliveryError(f"SMTP authentication failed: {exc}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientDeliveryError(f"SMTP delivery failed: {exc}") from exc
