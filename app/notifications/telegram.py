"""
Telegram delivery.

Telegram is the channel that actually matters here: a phone buzzes in seconds,
where email can sit on a server for minutes. For a seat that lasts under a
minute, that gap is the whole product.
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.notifications.base import (
    Message,
    PermanentDeliveryError,
    TransientDeliveryError,
)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        settings = get_settings()
        self.token = settings.telegram_bot_token
        self.api_base = settings.telegram_api_base.rstrip("/")
        self._transport = transport

    def send(self, destination: str, message: Message) -> None:
        """
        Post to the Bot API. `destination` is a Telegram chat id.

        Status codes are mapped to the two error kinds the retry logic
        understands:
          403 -- the user blocked the bot. Permanent; retrying is pointless.
          400 -- a malformed chat id. Permanent.
          429 -- rate limited by Telegram. Transient.
          5xx -- their problem, probably brief. Transient.
        """
        if not self.token:
            raise PermanentDeliveryError("TELEGRAM_BOT_TOKEN is not configured")

        url = f"{self.api_base}/bot{self.token}/sendMessage"
        payload = {"chat_id": destination, "text": message.body}

        try:
            with httpx.Client(timeout=10.0, transport=self._transport) as client:
                response = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise TransientDeliveryError(f"telegram request failed: {exc}") from exc

        if response.status_code == 200:
            return

        if response.status_code in (400, 403):
            raise PermanentDeliveryError(
                f"telegram rejected chat {destination}: HTTP {response.status_code}"
            )

        raise TransientDeliveryError(f"telegram returned HTTP {response.status_code}")
