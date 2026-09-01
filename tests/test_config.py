"""Settings validation and startup safety."""

from __future__ import annotations

import pytest

from app.config import Settings


def test_an_unknown_app_env_is_rejected():
    with pytest.raises(ValueError, match="APP_ENV"):
        Settings(APP_ENV="prod")


def test_a_zero_rate_limit_is_rejected():
    """A limit of 0 would silently stop every poll while looking configured."""
    with pytest.raises(ValueError):
        Settings(SCRAPE_REQUESTS_PER_WINDOW=0)


def test_production_refuses_to_start_with_no_notification_channel():
    """
    An alerting service running with every channel disabled is worse than one
    that failed to start: it looks healthy while telling nobody anything.
    """
    settings = Settings(
        APP_ENV="production",
        TELEGRAM_BOT_TOKEN="",
        SMTP_HOST="",
        API_KEY="k",
        SCHEDULE_BASE_URL="https://real.edu/schedule",
    )
    with pytest.raises(ValueError, match="notification channel"):
        settings.validate_production_safety()


def test_production_requires_an_api_key():
    settings = Settings(
        APP_ENV="production",
        TELEGRAM_BOT_TOKEN="t",
        API_KEY="",
        SCHEDULE_BASE_URL="https://real.edu/schedule",
    )
    with pytest.raises(ValueError, match="API_KEY"):
        settings.validate_production_safety()


def test_production_refuses_the_placeholder_schedule_url():
    settings = Settings(
        APP_ENV="production",
        TELEGRAM_BOT_TOKEN="t",
        API_KEY="k",
        SCHEDULE_BASE_URL="https://example.edu/course-schedule",
    )
    with pytest.raises(ValueError, match="SCHEDULE_BASE_URL"):
        settings.validate_production_safety()


def test_a_fully_configured_production_setup_passes():
    settings = Settings(
        APP_ENV="production",
        TELEGRAM_BOT_TOKEN="t",
        API_KEY="k",
        SCHEDULE_BASE_URL="https://real.edu/schedule",
    )
    settings.validate_production_safety()


def test_development_tolerates_the_defaults():
    Settings(APP_ENV="development").validate_production_safety()


def test_a_channel_reports_itself_enabled_when_configured():
    assert Settings(TELEGRAM_BOT_TOKEN="t").telegram_enabled is True
    assert Settings(TELEGRAM_BOT_TOKEN="").telegram_enabled is False
    assert Settings(SMTP_HOST="smtp.x").email_enabled is True
    assert Settings(SMTP_HOST="").email_enabled is False
