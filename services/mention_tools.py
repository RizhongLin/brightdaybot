"""
Tool definitions and executors for the @-mention Q&A agent.

The mention responder gives the model these tools so it can look up real
data (birthdays, upcoming birthdays, special days) instead of relying on
context stuffed into the prompt. Schemas use the Responses API flat shape:
{"type": "function", "name", "description", "parameters"}.

Privacy rules enforced here, not by the model:
- Opted-out (paused) users are reported as not found.
- Age/birth year is included only when the user has show_age enabled.

execute_tool() never raises — failures return an error JSON string so the
tool loop can always continue.
"""

import json
from datetime import datetime

from config import get_logger
from slack.client import get_username
from utils.date_utils import calculate_age, calculate_days_until_birthday

logger = get_logger("commands")

# Hard caps on tool output size (token control)
_MAX_BIRTHDAY_ENTRIES = 15
_MAX_SPECIAL_DAYS_PER_DATE = 3

MENTION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "get_user_birthday",
        "description": (
            "Look up one user's birthday by Slack user ID. Use the ID from a "
            "<@U...> mention in the question, or the asker's own ID for "
            "'my birthday' questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Slack user ID, e.g. 'U0123ABC' (from <@U0123ABC>)",
                }
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_upcoming_birthdays",
        "description": (
            "List upcoming birthdays of active birthday-channel members " "within the next N days."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 90,
                    "description": "Look-ahead window in days (default 7)",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_special_days",
        "description": "Get special days/observances for today and the next N days.",
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "description": "1 = today only (default 7)",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
]


def _normalize_user_id(raw) -> str:
    """Normalize '<@U123|bob>' / '<@U123>' / 'u123' to 'U123'."""
    user_id = str(raw or "").strip()
    if user_id.startswith("<@"):
        user_id = user_id[2:]
    user_id = user_id.rstrip(">")
    if "|" in user_id:
        user_id = user_id.split("|", 1)[0]
    return user_id.upper()


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _tool_get_user_birthday(args, app):
    from storage.birthdays import get_birthday, get_user_preferences, is_user_active

    user_id = _normalize_user_id(args.get("user_id"))
    if not user_id:
        return {"error": "user_id is required"}

    data = get_birthday(user_id)
    # Opted-out users are reported as not found — don't reveal opt-outs
    if not data or not is_user_active(user_id, data):
        return {"found": False}

    result = {
        "found": True,
        "username": get_username(app, user_id),
        "date": data.get("date"),
        "days_until": calculate_days_until_birthday(data.get("date"), datetime.now()),
    }

    year = data.get("year")
    if year and get_user_preferences(user_id).get("show_age", True):
        result["year"] = year
        result["age_turning"] = calculate_age(year) + 1

    return result


def _tool_get_upcoming_birthdays(args, app):
    from services.birthday_queries import get_upcoming_birthdays_grouped
    from storage.birthdays import load_birthdays

    days_ahead = _clamp(args.get("days_ahead"), 1, 90, 7)

    groups = get_upcoming_birthdays_grouped(load_birthdays(), app, limit=_MAX_BIRTHDAY_ENTRIES)

    entries = []
    for group in groups:
        if group["days_until"] > days_ahead:
            continue
        for person in group["people"]:
            entry = {
                "username": person.get("username"),
                "date": group["date_words"],
                "days_until": group["days_until"],
            }
            if person.get("year") and person.get("show_age", True):
                entry["age_turning"] = calculate_age(person["year"]) + 1
            entries.append(entry)
            if len(entries) >= _MAX_BIRTHDAY_ENTRIES:
                break
        if len(entries) >= _MAX_BIRTHDAY_ENTRIES:
            break

    return {"days_ahead": days_ahead, "birthdays": entries}


def _tool_get_special_days(args, app):
    from storage.special_days import get_upcoming_special_days

    days_ahead = _clamp(args.get("days_ahead"), 1, 30, 7)

    upcoming = get_upcoming_special_days(days_ahead=days_ahead, reference_date=datetime.now())

    result = {}
    for date_str, days in upcoming.items():
        result[date_str] = [
            {
                "name": d.name,
                "category": d.category,
                "description": (d.description or "")[:150],
            }
            for d in days[:_MAX_SPECIAL_DAYS_PER_DATE]
        ]

    return {"days_ahead": days_ahead, "special_days": result}


_TOOL_EXECUTORS = {
    "get_user_birthday": _tool_get_user_birthday,
    "get_upcoming_birthdays": _tool_get_upcoming_birthdays,
    "get_special_days": _tool_get_special_days,
}


def execute_tool(name: str, arguments_json: str, app) -> str:
    """
    Execute a tool call and return its result as a JSON string. Never raises.
    """
    try:
        args = json.loads(arguments_json) if arguments_json else {}
        if not isinstance(args, dict):
            args = {}
    except (TypeError, json.JSONDecodeError):
        args = {}

    executor = _TOOL_EXECUTORS.get(name)
    if executor is None:
        logger.warning(f"MENTION_TOOLS: Unknown tool requested: {name}")
        return json.dumps({"error": f"unknown tool: {name}"})

    try:
        return json.dumps(executor(args, app), ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"MENTION_TOOLS: Tool '{name}' failed: {e}")
        return json.dumps({"error": f"tool execution failed: {e}"})
