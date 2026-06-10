"""
Shared read-only birthday queries.

Extracted from the App Home handler so other consumers (App Home dashboard,
the @-mention tool agent) share one implementation of the privacy-relevant
filtering: only active users who are members of the birthday channel are
included, and `show_age` preferences are carried per person.
"""

from datetime import datetime

from config import APP_HOME_UPCOMING_BIRTHDAY_DATES, get_logger
from slack.client import get_username
from utils.date_utils import calculate_days_until_birthday

logger = get_logger("main")


def _safe_date_words(date_str):
    """Convert DD/MM to words, falling back to raw string on error."""
    from utils.date_utils import date_to_words

    try:
        return date_to_words(date_str)
    except (ValueError, TypeError):
        return date_str


def get_upcoming_birthdays_grouped(
    birthdays,
    app,
    limit=APP_HOME_UPCOMING_BIRTHDAY_DATES,
    channel_member_set=None,
    reference_date=None,
):
    """Get upcoming birthdays grouped by date for validated users only.

    Returns list of date groups: [{"date": "DD/MM", "date_words": "...", "days_until": N, "people": [...]}]
    Limited to `limit` unique dates (all people per date shown).
    """
    from storage.birthdays import is_user_active

    if reference_date is None:
        reference_date = datetime.now()
    flat = []

    if channel_member_set is None:
        from config import BIRTHDAY_CHANNEL
        from slack.client import get_channel_members

        channel_members = get_channel_members(app, BIRTHDAY_CHANNEL)
        channel_member_set = set(channel_members) if channel_members else set()

    for user_id, data in birthdays.items():
        if user_id not in channel_member_set:
            continue
        if not is_user_active(user_id, data):
            continue

        days = calculate_days_until_birthday(data["date"], reference_date)
        if days is not None:
            prefs = data.get("preferences", {})
            flat.append(
                {
                    "user_id": user_id,
                    "date": data["date"],
                    "year": data.get("year"),
                    "show_age": prefs.get("show_age", True),
                    "days_until": days,
                }
            )

    flat.sort(key=lambda x: x["days_until"])

    # Group by date, preserving sort order
    by_date = {}
    for entry in flat:
        by_date.setdefault(entry["date"], []).append(entry)

    # Take first N dates, resolve usernames only for displayed people
    result = []
    for date_str, people in list(by_date.items())[:limit]:
        for p in people:
            p["username"] = get_username(app, p["user_id"])
        result.append(
            {
                "date": date_str,
                "date_words": _safe_date_words(date_str),
                "days_until": people[0]["days_until"],
                "people": people,
            }
        )

    return result
