"""
Daily AI usage accounting for cost visibility.

Aggregates per-call token usage (already logged to ai.log) into a small
JSON file so the canvas dashboard can show "AI usage today" without log
parsing. Keys are UTC dates (matching the announcement-tracking convention);
entries older than the retention window are pruned on write.

Recording is strictly best-effort: failures are logged and swallowed so
accounting can never break message generation.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

from filelock import FileLock

from config import TIMEOUTS, TRACKING_DIR, get_logger

logger = get_logger("ai")

AI_USAGE_FILE = os.path.join(TRACKING_DIR, "ai_usage_daily.json")
AI_USAGE_RETENTION_DAYS = 30

_usage_lock = threading.Lock()


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_usage(path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"AI_USAGE: Tracking file unreadable, resetting: {e}")
        return {}


def _prune(usage: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=AI_USAGE_RETENTION_DAYS)).strftime(
        "%Y-%m-%d"
    )
    return {day: data for day, data in usage.items() if day >= cutoff}


def record_usage(context: str, input_tokens: int, output_tokens: int) -> None:
    """
    Add one API call's token usage to today's totals. Never raises.

    Args:
        context: Call-site label (e.g. "SPECIAL_DAY_MESSAGE")
        input_tokens: Prompt tokens for the call
        output_tokens: Completion tokens for the call
    """
    try:
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        context = str(context or "UNKNOWN")
        today = _today_key()

        with _usage_lock:
            lock = FileLock(AI_USAGE_FILE + ".lock", timeout=TIMEOUTS["file_lock"])
            with lock:
                usage = _load_usage(AI_USAGE_FILE)
                day = usage.setdefault(
                    today,
                    {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_context": {}},
                )
                day["calls"] += 1
                day["input_tokens"] += input_tokens
                day["output_tokens"] += output_tokens
                ctx = day["by_context"].setdefault(
                    context, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
                )
                ctx["calls"] += 1
                ctx["input_tokens"] += input_tokens
                ctx["output_tokens"] += output_tokens

                usage = _prune(usage)

                os.makedirs(TRACKING_DIR, exist_ok=True)
                with open(AI_USAGE_FILE, "w", encoding="utf-8") as f:
                    json.dump(usage, f, indent=2, sort_keys=True)
    except Exception as e:
        logger.warning(f"AI_USAGE: Failed to record usage (non-fatal): {e}")


def get_today_usage() -> dict:
    """
    Return today's aggregated AI usage.

    Returns:
        dict: {"calls", "input_tokens", "output_tokens", "by_context"} —
              zeros/empty when nothing was recorded today
    """
    empty = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_context": {}}
    try:
        usage = _load_usage(AI_USAGE_FILE)
        return usage.get(_today_key(), empty)
    except Exception as e:
        logger.warning(f"AI_USAGE: Failed to read usage (non-fatal): {e}")
        return empty
