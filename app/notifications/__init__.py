"""
Channel registry.

The task layer asks for a notifier by channel and gets one, so adding SMS
later means writing one class and adding one line here -- not touching the
polling logic or the task.
"""

from __future__ import annotations

from app.models import Channel
from app.notifications.base import (
    DeliveryError,
    Message,
    Notifier,
    PermanentDeliveryError,
    TransientDeliveryError,
    build_opening_message,
)
from app.notifications.email import EmailNotifier
from app.notifications.telegram import TelegramNotifier

__all__ = [
    "Channel",
    "DeliveryError",
    "EmailNotifier",
    "Message",
    "Notifier",
    "PermanentDeliveryError",
    "TelegramNotifier",
    "TransientDeliveryError",
    "build_opening_message",
    "get_notifier",
]


def get_notifier(channel: Channel) -> Notifier:
    if channel is Channel.telegram:
        return TelegramNotifier()
    if channel is Channel.email:
        return EmailNotifier()
    raise PermanentDeliveryError(f"no notifier registered for channel {channel}")
