"""
Shared pytest fixtures for BrightDayBot tests.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def reference_date():
    """Fixed reference date for deterministic testing: March 15, 2025"""
    return datetime(2025, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


# Slack API mocking fixtures


@pytest.fixture
def mock_slack_app():
    """Mock Slack app with client for testing API calls."""
    app = MagicMock()
    app.client = MagicMock()

    # Default successful responses
    app.client.users_profile_get.return_value = {
        "ok": True,
        "profile": {
            "display_name": "TestUser",
            "real_name": "Test User Full Name",
            "title": "Software Engineer",
            "image_512": "https://example.com/photo.jpg",
        },
    }
    app.client.users_info.return_value = {
        "ok": True,
        "user": {
            "tz": "America/New_York",
            "tz_label": "Eastern Standard Time",
            "tz_offset": -18000,
            "is_admin": False,
            "is_bot": False,
            "deleted": False,
        },
    }
    app.client.chat_postMessage.return_value = {
        "ok": True,
        "ts": "1234567890.123456",
        "channel": "C123456",
    }

    return app


@pytest.fixture
def mock_slack_client():
    """Standalone mock Slack client for direct client testing."""
    client = MagicMock()
    client.users_profile_get.return_value = {
        "ok": True,
        "profile": {"display_name": "TestUser", "real_name": "Test User"},
    }
    client.users_info.return_value = {
        "ok": True,
        "user": {"tz": "America/New_York", "is_admin": False},
    }
    client.chat_postMessage.return_value = {"ok": True, "ts": "1234567890.123456"}
    return client


@pytest.fixture
def slack_api_error():
    """Factory fixture for creating SlackApiError exceptions."""
    from slack_sdk.errors import SlackApiError

    def _create_error(error_code="channel_not_found"):
        response = MagicMock()
        response.data = {"error": error_code}
        return SlackApiError(message=error_code, response=response)

    return _create_error


# Birthday data fixtures


@pytest.fixture
def mock_birthday_data():
    """Factory fixture for creating mock birthday data with full JSON structure."""

    def _create_birthday(
        date="25/12",
        year=1990,
        active=True,
        image_enabled=True,
        show_age=True,
        celebration_style="standard",
        created_at="2025-01-01T00:00:00+00:00",
        updated_at="2025-01-01T00:00:00+00:00",
    ):
        return {
            "date": date,
            "year": year,
            "preferences": {
                "active": active,
                "image_enabled": image_enabled,
                "show_age": show_age,
                "celebration_style": celebration_style,
            },
            "created_at": created_at,
            "updated_at": updated_at,
        }

    return _create_birthday


@pytest.fixture
def sample_birthdays(mock_birthday_data):
    """Sample birthday data dict with multiple users."""
    return {
        "U001": mock_birthday_data(date="15/03", year=1990),
        "U002": mock_birthday_data(date="25/12", year=1985, show_age=False),
        "U003": mock_birthday_data(date="01/01", year=None, image_enabled=False),
        "U004": mock_birthday_data(date="29/02", year=2000, active=False),  # Paused user
    }


@pytest.fixture(autouse=True)
def _isolate_ai_usage(tmp_path, monkeypatch):
    """Keep AI usage accounting out of the real data/tracking directory.

    Mocked OpenAI responses in tests carry fake usage objects which would
    otherwise be recorded to the production tracking file.
    """
    import storage.ai_usage as ai_usage

    monkeypatch.setattr(ai_usage, "AI_USAGE_FILE", str(tmp_path / "ai_usage_daily.json"))
    monkeypatch.setattr(ai_usage, "TRACKING_DIR", str(tmp_path))
