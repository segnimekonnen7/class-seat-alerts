"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-05

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SECTION_STATUS = sa.Enum(
    "unknown", "open", "closed", name="sectionstatus", native_enum=False, length=16
)
CHANNEL = sa.Enum("telegram", "email", name="channel", native_enum=False, length=16)
NOTIFICATION_STATUS = sa.Enum(
    "pending",
    "sending",
    "sent",
    "failed",
    name="notificationstatus",
    native_enum=False,
    length=16,
)


def upgrade() -> None:
    op.create_table(
        "sections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("term", sa.String(length=16), nullable=False),
        sa.Column("crn", sa.String(length=16), nullable=False),
        sa.Column("course_code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("instructor", sa.String(length=255), nullable=False),
        sa.Column("status", SECTION_STATUS, nullable=False),
        sa.Column("seats_open", sa.Integer(), nullable=False),
        sa.Column("seats_total", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # A CRN is only unique within a term, so the pair is what is unique.
        sa.UniqueConstraint("term", "crn", name="uq_sections_term_crn"),
    )
    # The scheduler's only query is "what is due", so it gets its own index.
    op.create_index("ix_sections_due", "sections", ["status", "next_check_at"], unique=False)

    op.create_table(
        "watches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("section_id", sa.Integer(), nullable=False),
        sa.Column("channel", CHANNEL, nullable=False),
        sa.Column("destination", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "section_id", "channel", "destination", name="uq_watches_section_channel_dest"
        ),
    )
    op.create_index(op.f("ix_watches_section_id"), "watches", ["section_id"], unique=False)
    op.create_index(op.f("ix_watches_destination"), "watches", ["destination"], unique=False)

    op.create_table(
        "status_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("section_id", sa.Integer(), nullable=False),
        sa.Column("from_status", SECTION_STATUS, nullable=False),
        sa.Column("to_status", SECTION_STATUS, nullable=False),
        sa.Column("seats_open", sa.Integer(), nullable=False),
        sa.Column("seats_total", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["section_id"], ["sections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_status_events_section_id"), "status_events", ["section_id"], unique=False
    )
    op.create_index(
        op.f("ix_status_events_observed_at"), "status_events", ["observed_at"], unique=False
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("watch_id", sa.Integer(), nullable=False),
        sa.Column("status_event_id", sa.Integer(), nullable=False),
        sa.Column("channel", CHANNEL, nullable=False),
        sa.Column("destination", sa.String(length=255), nullable=False),
        sa.Column("status", NOTIFICATION_STATUS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["status_event_id"], ["status_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["watch_id"], ["watches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # This is the "once per closed-to-open transition" guarantee. A retried
        # task cannot insert a second alert for the same opening.
        sa.UniqueConstraint("watch_id", "status_event_id", name="uq_notifications_watch_event"),
    )
    op.create_index(op.f("ix_notifications_watch_id"), "notifications", ["watch_id"], unique=False)
    op.create_index(
        op.f("ix_notifications_status_event_id"),
        "notifications",
        ["status_event_id"],
        unique=False,
    )
    op.create_index(op.f("ix_notifications_status"), "notifications", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_status"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_status_event_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_watch_id"), table_name="notifications")
    op.drop_table("notifications")

    op.drop_index(op.f("ix_status_events_observed_at"), table_name="status_events")
    op.drop_index(op.f("ix_status_events_section_id"), table_name="status_events")
    op.drop_table("status_events")

    op.drop_index(op.f("ix_watches_destination"), table_name="watches")
    op.drop_index(op.f("ix_watches_section_id"), table_name="watches")
    op.drop_table("watches")

    op.drop_index("ix_sections_due", table_name="sections")
    op.drop_table("sections")
