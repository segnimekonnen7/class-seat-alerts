"""
The notifier contract.

Every channel answers one question the retry logic depends on: was this
failure worth trying again? A Telegram 500 is; a Telegram 403 because the user
blocked the bot is not, and retrying it forever just fills the queue.

So a failed send raises either TransientDeliveryError or PermanentDeliveryError,
and the task layer never has to know what a Telegram status code means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DeliveryError(RuntimeError):
    """Base for anything that went wrong sending a message."""


class TransientDeliveryError(DeliveryError):
    """A timeout, a 5xx, a rate limit. Worth another attempt."""


class PermanentDeliveryError(DeliveryError):
    """A bad address, a blocked bot. Another attempt changes nothing."""


@dataclass(frozen=True)
class Message:
    """
    What gets delivered.

    `subject` is only used by email; Telegram ignores it. Building one message
    and letting each channel take what it needs beats formatting the same
    opening twice.
    """

    subject: str
    body: str


class Notifier(Protocol):
    """A delivery channel."""

    name: str

    def send(self, destination: str, message: Message) -> None:
        """Deliver, or raise a Transient/Permanent DeliveryError."""
        ...


def build_opening_message(
    *,
    course_code: str,
    title: str,
    crn: str,
    term: str,
    seats_open: int,
    seats_total: int,
) -> Message:
    """
    The alert itself.

    It leads with the CRN because that is the thing a student types into the
    registration form. The whole value of this service is the seconds between
    reading the alert and submitting the form, and making someone go look up
    the CRN spends them.
    """
    heading = f"{course_code} is open".strip()
    seats = f"{seats_open} of {seats_total} seats" if seats_total else f"{seats_open} seats"

    body = (
        f"{course_code} - {title}\n"
        f"CRN {crn} ({term}) just opened.\n"
        f"{seats} available.\n\n"
        f"Register now -- these go fast."
    )
    return Message(subject=heading, body=body)
