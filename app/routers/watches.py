"""
Add, list, and remove watched sections.

The one thing worth reading closely is what happens when a watch is created on
a section nobody has tracked before: the section row is created in the
`unknown` status, not `closed`. Seeding it as closed would make the first
successful poll look like a closed -> open transition and fire an alert for a
section that was open the whole time -- the exact false positive that makes
someone mute the bot.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.deps import require_api_key
from app.models import Section, SectionStatus, Watch
from app.schemas import WatchCreate, WatchOut, WatchWithSection

router = APIRouter(prefix="/watches", tags=["watches"])


def _get_or_create_section(db: Session, term: str, crn: str) -> Section:
    section = (
        db.execute(select(Section).where(Section.term == term, Section.crn == crn))
        .scalars()
        .first()
    )
    if section is not None:
        return section

    # status defaults to `unknown`, and next_check_at to now, so the poller
    # picks it up on its next pass and establishes a real baseline.
    section = Section(term=term, crn=crn, status=SectionStatus.unknown)
    db.add(section)
    db.flush()
    return section


@router.post("", response_model=WatchOut, status_code=status.HTTP_201_CREATED)
def create_watch(
    payload: WatchCreate,
    response: Response,
    db: Session = Depends(get_db),
    _: None = Depends(require_api_key),
) -> Watch:
    """
    Start watching a section.

    Submitting the same watch twice returns the existing one with a 200 instead
    of erroring -- students do double-tap the button, and the second tap should
    read as "yes, you are watching this" rather than as a failure.
    """
    settings = get_settings()

    existing_count = db.execute(
        select(func.count())
        .select_from(Watch)
        .where(Watch.destination == payload.destination, Watch.active.is_(True))
    ).scalar_one()

    if existing_count >= settings.max_watches_per_destination:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"This destination already has {existing_count} active watches "
                f"(limit {settings.max_watches_per_destination})."
            ),
        )

    section = _get_or_create_section(db, payload.term, payload.crn)

    watch = Watch(
        section_id=section.id,
        channel=payload.channel,
        destination=payload.destination,
        label=payload.label,
        active=True,
    )
    db.add(watch)

    try:
        db.flush()
    except IntegrityError:
        # The unique constraint caught a duplicate: same section, channel, and
        # destination. Hand back the row that already exists.
        db.rollback()
        duplicate = (
            db.execute(
                select(Watch).where(
                    Watch.section_id == section.id,
                    Watch.channel == payload.channel,
                    Watch.destination == payload.destination,
                )
            )
            .scalars()
            .one()
        )
        if not duplicate.active:
            duplicate.active = True
            db.commit()
            db.refresh(duplicate)
        response.status_code = status.HTTP_200_OK
        return duplicate

    db.commit()
    db.refresh(watch)
    return watch


@router.get("", response_model=list[WatchWithSection])
def list_watches(
    db: Session = Depends(get_db),
    destination: str | None = Query(default=None),
    active_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[Watch]:
    """
    List watches, newest first.

    selectinload pulls the sections in one extra query rather than one per
    watch -- a 50-row page would otherwise cost 51 round trips.
    """
    stmt = (
        select(Watch)
        .options(selectinload(Watch.section))
        .order_by(Watch.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if destination is not None:
        stmt = stmt.where(Watch.destination == destination)
    if active_only:
        stmt = stmt.where(Watch.active.is_(True))

    return list(db.execute(stmt).scalars().all())


@router.get("/{watch_id}", response_model=WatchWithSection)
def get_watch(watch_id: int, db: Session = Depends(get_db)) -> Watch:
    watch = db.get(Watch, watch_id)
    if watch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watch not found")
    return watch


@router.delete("/{watch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watch(
    watch_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_api_key),
) -> Response:
    """
    Stop watching.

    Deactivated rather than deleted: the notifications already sent reference
    this watch, and the history of what was sent to whom is worth more than the
    row is worth reclaiming.
    """
    watch = db.get(Watch, watch_id)
    if watch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watch not found")

    watch.active = False
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
