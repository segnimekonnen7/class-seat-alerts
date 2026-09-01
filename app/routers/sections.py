"""
Reading section state and history.

Read-only. Sections are created as a side effect of somebody watching them,
never directly -- a section with no watcher is a section the poller would be
spending the school's goodwill on for nobody.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Notification, Section, SectionStatus, StatusEvent
from app.schemas import NotificationOut, SectionOut, StatusEventOut

router = APIRouter(prefix="/sections", tags=["sections"])


@router.get("", response_model=list[SectionOut])
def list_sections(
    db: Session = Depends(get_db),
    term: str | None = Query(default=None),
    section_status: SectionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Section]:
    stmt = select(Section).order_by(Section.term, Section.crn).limit(limit).offset(offset)
    if term is not None:
        stmt = stmt.where(Section.term == term)
    if section_status is not None:
        stmt = stmt.where(Section.status == section_status)
    return list(db.execute(stmt).scalars().all())


@router.get("/{section_id}", response_model=SectionOut)
def get_section(section_id: int, db: Session = Depends(get_db)) -> Section:
    section = db.get(Section, section_id)
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Section not found")
    return section


@router.get("/{section_id}/events", response_model=list[StatusEventOut])
def list_section_events(
    section_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[StatusEvent]:
    """
    Every status change observed for this section, newest first.

    Useful beyond debugging: it is the data that answers "does this section
    ever actually open, and for how long", which tells a student whether to
    keep waiting or take the 8am one.
    """
    if db.get(Section, section_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Section not found")

    stmt = (
        select(StatusEvent)
        .where(StatusEvent.section_id == section_id)
        .order_by(StatusEvent.id.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


@router.get("/{section_id}/notifications", response_model=list[NotificationOut])
def list_section_notifications(
    section_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Notification]:
    """What was sent, to whom, and whether it landed."""
    if db.get(Section, section_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Section not found")

    stmt = (
        select(Notification)
        .join(StatusEvent, Notification.status_event_id == StatusEvent.id)
        .where(StatusEvent.section_id == section_id)
        .order_by(Notification.id.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())
