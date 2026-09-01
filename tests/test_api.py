"""API tests: adding, listing, and removing watched sections."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import Section, SectionStatus, Watch
from tests.conftest import CRN, TERM, make_section, make_watch


def watch_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "term": TERM,
        "crn": CRN,
        "channel": "telegram",
        "destination": "123456789",
        "label": "Need this to graduate",
    }
    payload.update(overrides)
    return payload


# --- Creating --------------------------------------------------------------


def test_creating_a_watch_returns_201(client):
    response = client.post("/watches", json=watch_payload())
    assert response.status_code == 201
    assert response.json()["destination"] == "123456789"


def test_creating_a_watch_creates_the_section(client, db):
    client.post("/watches", json=watch_payload())
    section = db.execute(select(Section).where(Section.crn == CRN)).scalars().one()

    assert section.term == TERM


def test_a_new_section_starts_unknown_not_closed(client, db):
    """
    Seeding it as `closed` would make the first successful poll look like a
    closed -> open transition and fire an alert for a section that was open the
    whole time -- the false positive that makes someone mute the bot.
    """
    client.post("/watches", json=watch_payload())
    section = db.execute(select(Section).where(Section.crn == CRN)).scalars().one()

    assert section.status is SectionStatus.unknown


def test_a_new_section_is_due_immediately(client, db):
    """Otherwise the student waits a full interval before anything is checked."""
    client.post("/watches", json=watch_payload())
    section = db.execute(select(Section).where(Section.crn == CRN)).scalars().one()

    assert section.is_due


def test_two_watches_on_one_section_share_the_section(client, db):
    client.post("/watches", json=watch_payload(destination="111"))
    client.post("/watches", json=watch_payload(destination="222"))

    assert db.execute(select(func.count()).select_from(Section)).scalar_one() == 1
    assert db.execute(select(func.count()).select_from(Watch)).scalar_one() == 2


def test_submitting_the_same_watch_twice_returns_the_existing_one(client, db):
    """Students do double-tap the button; the second tap is not an error."""
    first = client.post("/watches", json=watch_payload())
    second = client.post("/watches", json=watch_payload())

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert db.execute(select(func.count()).select_from(Watch)).scalar_one() == 1


def test_rewatching_reactivates_a_removed_watch(client, db):
    created = client.post("/watches", json=watch_payload()).json()
    client.delete(f"/watches/{created['id']}")

    again = client.post("/watches", json=watch_payload())
    assert again.json()["active"] is True
    assert again.json()["id"] == created["id"]


# --- Validation ------------------------------------------------------------


def test_an_email_destination_must_be_an_email(client):
    """
    Caught at submission rather than discovered as a permanent delivery failure
    at 2am when the section finally opens.
    """
    response = client.post("/watches", json=watch_payload(channel="email", destination="123456789"))
    assert response.status_code == 422


def test_a_telegram_destination_must_be_a_chat_id(client):
    response = client.post(
        "/watches", json=watch_payload(channel="telegram", destination="me@example.com")
    )
    assert response.status_code == 422


def test_a_valid_email_watch_is_accepted(client):
    response = client.post(
        "/watches", json=watch_payload(channel="email", destination="me@example.com")
    )
    assert response.status_code == 201


def test_a_negative_telegram_group_id_is_accepted(client):
    """Telegram group chat ids are negative integers."""
    response = client.post("/watches", json=watch_payload(destination="-1001234567"))
    assert response.status_code == 201


def test_a_non_numeric_crn_is_rejected(client):
    response = client.post("/watches", json=watch_payload(crn="CS320"))
    assert response.status_code == 422


def test_an_unknown_channel_is_rejected(client):
    response = client.post("/watches", json=watch_payload(channel="carrier-pigeon"))
    assert response.status_code == 422


def test_the_per_destination_watch_limit_is_enforced(client, db):
    """One person cannot queue unbounded work against the school's servers."""
    section = make_section(db)
    for index in range(25):
        make_watch(db, section, destination="123456789") if index == 0 else None

    for index in range(1, 25):
        client.post("/watches", json=watch_payload(crn=str(20000 + index)))

    response = client.post("/watches", json=watch_payload(crn="99999"))
    assert response.status_code == 429


# --- Listing and removing --------------------------------------------------


def test_listing_returns_watches_with_their_section(client):
    client.post("/watches", json=watch_payload())
    body = client.get("/watches").json()

    assert len(body) == 1
    assert body[0]["section"]["crn"] == CRN


def test_listing_can_be_filtered_by_destination(client):
    client.post("/watches", json=watch_payload(destination="111"))
    client.post("/watches", json=watch_payload(crn="20001", destination="222"))

    body = client.get("/watches?destination=222").json()
    assert [w["destination"] for w in body] == ["222"]


def test_removing_a_watch_deactivates_it(client, db):
    created = client.post("/watches", json=watch_payload()).json()
    assert client.delete(f"/watches/{created['id']}").status_code == 204

    watch = db.get(Watch, created["id"])
    assert watch is not None and watch.active is False


def test_a_removed_watch_is_hidden_from_the_default_listing(client):
    created = client.post("/watches", json=watch_payload()).json()
    client.delete(f"/watches/{created['id']}")

    assert client.get("/watches").json() == []
    assert len(client.get("/watches?active_only=false").json()) == 1


def test_removing_an_unknown_watch_is_a_404(client):
    assert client.delete("/watches/9999").status_code == 404


def test_a_missing_watch_is_a_404(client):
    assert client.get("/watches/9999").status_code == 404


# --- Sections --------------------------------------------------------------


def test_sections_can_be_listed(client, db):
    make_section(db)
    assert len(client.get("/sections").json()) == 1


def test_sections_can_be_filtered_by_status(client, db):
    make_section(db, crn="1", status=SectionStatus.open)
    make_section(db, crn="2", status=SectionStatus.closed)

    body = client.get("/sections?status=open").json()
    assert [s["crn"] for s in body] == ["1"]


def test_a_missing_section_is_a_404(client):
    assert client.get("/sections/9999").status_code == 404


def test_section_events_are_returned_newest_first(client, db):
    from app.models import StatusEvent

    section = make_section(db)
    for _ in range(3):
        db.add(
            StatusEvent(
                section_id=section.id,
                from_status=SectionStatus.closed,
                to_status=SectionStatus.open,
            )
        )
    db.commit()

    ids = [e["id"] for e in client.get(f"/sections/{section.id}/events").json()]
    assert ids == sorted(ids, reverse=True)


def test_events_for_a_missing_section_are_a_404(client):
    assert client.get("/sections/9999/events").status_code == 404


def test_section_notifications_can_be_read(client, db):
    from app.polling import queue_notifications, record_observation
    from tests.test_polling import observation

    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)
    event = record_observation(db, section, observation(status=SectionStatus.open))
    queue_notifications(db, event)
    db.commit()

    body = client.get(f"/sections/{section.id}/notifications").json()
    assert len(body) == 1
    assert body[0]["status"] == "pending"


# --- Health ----------------------------------------------------------------


def test_health_does_not_touch_dependencies(client):
    """
    Liveness must not depend on Postgres or Redis, or an outage gets the
    container killed and restarted straight back into the same outage.
    """
    assert client.get("/health").json() == {"status": "ok"}


def test_readyz_checks_both_dependencies(client):
    body = client.get("/readyz").json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    assert body["redis"] == "ok"


def test_stats_reports_the_scrape_budget(client, db):
    make_section(db, status=SectionStatus.open)
    body = client.get("/stats").json()

    assert body["sections"]["open"] == 1
    assert body["scrape_window_limit"] >= 1
    assert "scrape_window_used" in body
