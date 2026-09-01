"""
Request and response shapes.

The interesting validation is on `destination`, because what counts as a valid
destination depends on the channel: an email address for email, a numeric chat
id for Telegram. Catching that here means a bad value is a 422 at submission
rather than a permanent delivery failure discovered at 2am when the section
finally opens.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Channel, NotificationStatus, SectionStatus

# A Telegram chat id is an integer, negative for groups.
_TELEGRAM_CHAT_ID = re.compile(r"^-?\d{1,20}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class WatchCreate(BaseModel):
    term: str = Field(min_length=1, max_length=16)
    crn: str = Field(min_length=1, max_length=16)
    channel: Channel
    destination: str = Field(min_length=1, max_length=255)
    label: str = Field(default="", max_length=128)

    @field_validator("term", "crn")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("crn")
    @classmethod
    def crn_is_digits(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("CRN must be numeric")
        return value

    @model_validator(mode="after")
    def destination_matches_channel(self) -> WatchCreate:
        destination = self.destination.strip()

        if self.channel is Channel.email and not _EMAIL.match(destination):
            raise ValueError("destination must be an email address for the email channel")

        if self.channel is Channel.telegram and not _TELEGRAM_CHAT_ID.match(destination):
            raise ValueError(
                "destination must be a numeric Telegram chat id for the telegram channel"
            )

        object.__setattr__(self, "destination", destination)
        return self


class SectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    term: str
    crn: str
    course_code: str
    title: str
    instructor: str
    status: SectionStatus
    seats_open: int
    seats_total: int
    last_checked_at: datetime | None
    last_status_change_at: datetime | None
    next_check_at: datetime
    consecutive_failures: int


class WatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    section_id: int
    channel: Channel
    destination: str
    label: str
    active: bool
    created_at: datetime


class WatchWithSection(WatchOut):
    section: SectionOut


class StatusEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    section_id: int
    from_status: SectionStatus
    to_status: SectionStatus
    seats_open: int
    seats_total: int
    observed_at: datetime


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    watch_id: int
    status_event_id: int
    channel: Channel
    destination: str
    status: NotificationStatus
    attempts: int
    last_error: str
    created_at: datetime
    sent_at: datetime | None


class HealthOut(BaseModel):
    status: str


class ReadinessOut(BaseModel):
    status: str
    database: str
    redis: str
