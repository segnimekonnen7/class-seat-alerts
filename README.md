# Class Seat Alerts

A background service that watches class sections on the university course
schedule and sends a Telegram or email alert the moment a full section opens —
so you can register before it fills again.

Built with Python, FastAPI, PostgreSQL, Celery, Redis, and Docker.

[![CI](https://github.com/segnimekonnen7/class-seat-alerts/actions/workflows/ci.yml/badge.svg)](https://github.com/segnimekonnen7/class-seat-alerts/actions/workflows/ci.yml)

---

## The problem

A seat in a full section opens when somebody drops. It lasts minutes,
sometimes seconds. Refreshing the schedule page yourself means either watching
it all day or missing it.

So: watch it for you, and push a notification the instant it changes.

```
celery beat  --every 15s-->  dispatch_due_sections
                                  |
                                  |  one message per section that is due
                                  |  AND has somebody waiting on it
                                  v
                            poll_section
                                  |
                    take a slot from the shared rate limiter
                                  |
                          fetch + parse the page
                                  |
                    status changed?  --no-->  reschedule, done
                                  | yes
                                  v
                        write a status_event row
                                  |
                    closed -> open?  --no-->  recorded, nobody told
                                  | yes
                                  v
                    one notification row per active watch
                    (unique on watch_id + status_event_id)
                                  |
                                  v
                        send_notification  ->  Telegram / email
```

---

## The one guarantee

> **An alert fires on a closed → open transition. Once. Per watch.**

Every design decision in the service comes out of that sentence, and each
clause rules out an obvious-but-wrong implementation:

**"on a transition"** — not "while the section is open". Alerting on
`seats_open > 0` would re-fire every poll for as long as the seat lasted. A
transition is a discrete event, so it gets a row in `status_events`, and a
notification points at that row.

**"closed → open"** — not "→ open". A brand-new watch on a section that was
already open is not an opening; nobody observed it change. That is why a
section starts as `unknown` rather than `closed`. Seeding it as `closed` would
make the very first poll look like an opening and fire a false alert — the
kind that makes someone mute the bot before it's ever useful.

**"once"** — Celery runs with `task_acks_late`, so a worker killed mid-task
puts its message back and the task runs again. That is deliberate (losing a
poll is worse than repeating one), and it means the *code* cannot assume it
runs once. Two things make the duplicate harmless:

- a unique constraint on `(watch_id, status_event_id)` — the second insert has
  nowhere to go
- a conditional claim — `UPDATE … WHERE status = 'pending'`, not a
  read-then-write, so two workers handed the same message cannot both decide
  they own it

**"per watch"** — the constraint is per watch and per event, so a section that
opens, closes, and opens again correctly alerts the same person twice.

---

## Being a good citizen

The school's servers are not mine to hammer.

- **A sliding-window rate limiter in Redis**, not per-process. Whether one
  worker is running or ten, the school sees one bounded request rate. A
  fixed-window counter would let a caller spend its whole budget in the last
  second of one window and the next budget in the first second of the
  following one — a burst of double the configured rate, aimed at exactly the
  servers I promised not to hammer.
- **Adaptive intervals.** Closed sections are checked every 60s — those are the
  ones that might open. Open sections get 600s; they're only tracked so a later
  close-then-open is caught properly.
- **Only watched sections are polled.** A section whose last watch was removed
  stays in the table for its history but stops costing requests.
- **Exponential backoff with jitter** on failure, capped at an hour. Without
  jitter, a hundred sections that failed together during one outage would all
  retry at the same instant and reproduce it.
- **A real User-Agent**, with a contact address.

---

## Running it

```bash
docker compose up --build
```

That brings up Postgres, Redis, the API, a Celery worker, and beat. The API is
on <http://localhost:8000>, docs at `/docs`.

Locally, without Docker:

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # then edit it
alembic upgrade head

uvicorn app.main:app --reload                                        # terminal 1
celery -A app.tasks.celery_app.celery_app worker --loglevel=info     # terminal 2
celery -A app.tasks.celery_app.celery_app beat   --loglevel=info     # terminal 3
```

### Watching a section

```bash
curl -X POST localhost:8000/watches \
  -H "Content-Type: application/json" \
  -d '{
        "term": "20273",
        "crn": "002948",
        "channel": "telegram",
        "destination": "123456789",
        "label": "Need this to graduate"
      }'
```

That creates the section if nobody was watching it yet, marks it due for an
immediate check, and returns the watch. Submit it again and you get `200` with
the same watch back rather than a duplicate — students do double-tap the button.

`destination` is validated against the channel: an email address for `email`, a
numeric chat id for `telegram`. Catching that here means a typo is a 422 at
submission, not a permanent delivery failure discovered at 2am when the section
finally opens.

---

## API

| Method | Path | What |
|---|---|---|
| `POST` | `/watches` | Start watching a section |
| `GET` | `/watches` | List watches, with their sections |
| `GET` | `/watches/{id}` | One watch |
| `DELETE` | `/watches/{id}` | Stop watching (deactivates, keeps history) |
| `GET` | `/sections` | Tracked sections and their current state |
| `GET` | `/sections/{id}/events` | Every status change observed |
| `GET` | `/sections/{id}/notifications` | What was sent, to whom, and whether it landed |
| `GET` | `/health` | Liveness — touches nothing |
| `GET` | `/readyz` | Readiness — checks Postgres and Redis |
| `GET` | `/stats` | Counts, plus the scrape budget in use |

Write endpoints require an `X-API-Key` header when `API_KEY` is set — unset
locally so the thing is easy to run, and required in production, where startup
refuses to proceed without one.

`/health` never touches a dependency: otherwise a Postgres outage gets the
container killed and restarted straight back into the same outage.

`scrape_window_used` on `/stats` is the number worth watching. If it is pinned
at the limit, more sections are being watched than the polite request budget
covers, and detection lag is growing even though nothing is erroring.

---

## Data model

| Table | Holds |
|---|---|
| `sections` | A tracked section, its seat count, and when it is next due |
| `watches` | One person's interest in one section |
| `status_events` | Append-only log of every observed status change |
| `notifications` | One row per (watch, event) — the idempotency key |

`status_events` keeps open → closed transitions too, even though nobody is
notified about them. That history is what answers "does this section ever
actually open, and for how long" — which tells a student whether to keep
waiting or take the 8am one.

---

## The scraper

It targets the real thing: <https://secure2.mnsu.edu/ClassSchedule/>. The
fixtures in `tests/fixtures/` are actual saved responses, and the parser is
verified against them.

**Fetching is a form POST, not a URL.** The schedule is an ASP.NET MVC app: you
GET the page for a hidden `__RequestVerificationToken`, then POST the search
with that token and the matching session cookie. Both have to come from the
same client or the search is rejected. Searching by Course ID returns one
section (~23KB) instead of a whole subject (~205KB) — a tenth of the bytes off
the school's servers per check.

**The status CSS classes on the real page are inverted.** This is the trap that
would have quietly broken everything:

```html
<span class="openSession">Closed</span>      <!-- 4 on the sample page -->
<span class="CloseSession">Open</span>       <!-- 56 on the sample page -->
```

Every single one. A parser keying on `class="openSession"` would read the whole
schedule backwards — alerting the moment a section *closed* and staying silent
when one opened. The status comes from the span's **text**; the class is never
consulted. `test_the_status_classes_on_the_real_page_are_inverted` pins this to
the saved page so it is documented by a failing test, not just a comment.

**There is no seats-available column.** The page gives Size and Enrl; open
seats is the difference, floored at zero because a section can be enrolled past
its cap.

**Columns are found by header name, not position.** The 13 columns are
identical across every table today, but positional indexing fails *silently* if
MNSU inserts one — you would read Bldg/Room as the instructor and Enrl as the
capacity, and the numbers would still parse. Reading the header row first means
a layout change raises `ScheduleFormatChanged` instead of producing confident
wrong answers.

**A missing Course ID is a different error from a failed fetch.** A removed id
should stop being polled; a network blip should be retried. An SSO redirect or
maintenance page served with a 200 is treated as the site being down, not as
"your section is gone".

MNSU labels the section identifier "Course ID" (e.g. `002948`); that is this
system's `crn`, the same concept under the name most schools use. Terms are
MNSU `yrtr` codes — `20273` is Fall 2026.

---

## Tests

```bash
pytest
```

143 tests against in-memory SQLite and fakeredis — no services needed. They're
organized around the ways the guarantee could break:

- **The real pages** (`test_parser.py`) — every assertion runs against saved
  responses from secure2.mnsu.edu, including one that pins the inverted CSS
  classes so the trap can never be silently "fixed".
- **The state machine** (`test_polling.py`) — every transition, including the
  negatives: an unchanged poll fires nothing, `unknown → open` is not an
  opening, a failed poll does not change the status (a timeout is not evidence
  a section closed, and overwriting it would manufacture a fake transition on
  the next successful poll).
- **Idempotency** — queueing twice creates nothing new; a duplicate does not
  block the other watchers; a claimed notification cannot be claimed again.
- **The rate limiter** — including the burst a fixed-window counter would let
  through, and that two limiter instances share one budget.
- **Both channels** — that each failure is classified retryable or permanent
  correctly. Getting that backwards means either retrying a blocked bot forever
  or giving up on a five-second outage.
- **The full path** (`test_tasks.py`) — poll → transition → delivery, including
  that polling three times after an opening still sends exactly one alert.

## CI

Three jobs on every push and PR:

- **quality** — Ruff (lint + format), mypy over `app/`, full test suite
- **migrations** — against a real Postgres 16: apply, then `alembic check` for
  model drift, then downgrade to base
- **celery** — loads the Celery app against a real Redis and asserts every task
  is registered and every beat entry points at one that exists. A typo in a
  task name otherwise only shows up when a section opens and nothing fires.

## Layout

```
app/
  config.py           typed settings; refuses to boot production that can't deliver
  db.py               engine, request session, and session_scope for workers
  models.py           the four tables
  schemas.py          validation, including destination-vs-channel
  deps.py             the optional API key guard
  rate_limit.py       sliding-window limiter, shared via Redis
  polling.py          the state machine -- no Celery, no HTTP, fully testable
  scraper/
    client.py         the MNSU token + POST flow, rate limited and timed out
    parser.py         real MNSU HTML -> Observation, a pure function
  notifications/
    base.py           the Notifier contract and transient/permanent errors
    telegram.py       Bot API
    email.py          SMTP
  tasks/
    celery_app.py     acks_late, prefetch=1, beat schedule
    poll.py           dispatch / poll / send / sweep
  routers/            watches, sections, health
tests/                143 tests, incl. real saved MNSU pages
```
