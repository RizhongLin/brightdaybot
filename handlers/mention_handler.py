"""
Mention Handler for BrightDayBot

Handles @-mention events to answer questions about:
- Special days and observances
- Upcoming birthdays
- General queries about the bot's capabilities

Includes rate limiting to prevent abuse.
"""

import re
import time
from collections import defaultdict
from typing import Dict, Tuple

from slack_sdk.errors import SlackApiError

from config import get_logger

logger = get_logger("events")


class RateLimiter:
    """Simple in-memory rate limiter for @-mentions."""

    def __init__(self, window_seconds: int = 60, max_requests: int = 5):
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._requests: Dict[str, list] = defaultdict(list)

    def is_allowed(self, user_id: str) -> Tuple[bool, int]:
        """
        Check if user is allowed to make a request.

        Args:
            user_id: User ID to check

        Returns:
            Tuple of (is_allowed, seconds_until_reset)
        """
        now = time.time()
        window_start = now - self.window_seconds

        # Clean old requests
        self._requests[user_id] = [ts for ts in self._requests[user_id] if ts > window_start]

        # Check limit
        if len(self._requests[user_id]) >= self.max_requests:
            oldest = min(self._requests[user_id])
            seconds_until_reset = int(oldest + self.window_seconds - now) + 1
            return False, seconds_until_reset

        # Record this request
        self._requests[user_id].append(now)
        return True, 0


# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        from config import MENTION_RATE_LIMIT_MAX, MENTION_RATE_LIMIT_WINDOW

        _rate_limiter = RateLimiter(
            window_seconds=MENTION_RATE_LIMIT_WINDOW,
            max_requests=MENTION_RATE_LIMIT_MAX,
        )
    return _rate_limiter


def classify_question(text: str) -> str:
    """
    Classify the type of question being asked.

    Args:
        text: The mention text (with bot mention removed)

    Returns:
        One of: 'special_days', 'birthdays', 'upcoming', 'help', 'general'
    """
    text_lower = text.lower()

    # Special days keywords
    special_keywords = [
        "special day",
        "special days",
        "observance",
        "holiday",
        "international day",
        "world day",
        "un day",
        "today's day",
        "what day is",
        "what is today",
    ]
    for keyword in special_keywords:
        if keyword in text_lower:
            return "special_days"

    # Birthday keywords
    birthday_keywords = [
        "birthday",
        "birthdays",
        "born",
        "celebrate",
        "upcoming birthday",
        "next birthday",
        "whose birthday",
        "who has a birthday",
    ]
    for keyword in birthday_keywords:
        if keyword in text_lower:
            return "birthdays"

    # Upcoming events
    upcoming_keywords = [
        "upcoming",
        "coming up",
        "next week",
        "this week",
        "soon",
        "schedule",
        "calendar",
    ]
    for keyword in upcoming_keywords:
        if keyword in text_lower:
            return "upcoming"

    # Help/capabilities
    help_keywords = [
        "help",
        "what can you",
        "how do you",
        "what do you",
        "commands",
        "features",
    ]
    for keyword in help_keywords:
        if keyword in text_lower:
            return "help"

    return "general"


def handle_mention(app, event: dict, say, bot_user_id: str = None) -> dict:
    """
    Handle an @-mention of the bot.

    Args:
        app: Slack app instance
        event: The app_mention event
        say: Say function for responding
        bot_user_id: The bot's own user ID. When provided, only the bot's
            mention is stripped so user mentions like <@U123> survive into
            the question (needed for "when is <@U123>'s birthday?").

    Returns:
        Dict with results: {"responded": bool, "question_type": str, "error": str or None}
    """
    result = {"responded": False, "question_type": None, "error": None}

    try:
        from config import MENTION_QA_ENABLED

        if not MENTION_QA_ENABLED:
            logger.debug("MENTION: Q&A is disabled")
            return result

        user_id = event.get("user")
        text = event.get("text", "")
        channel = event.get("channel")
        # Always reply in a thread to avoid channel clutter:
        # - If mention is in a thread, reply in that thread (thread_ts)
        # - If mention is in main channel, start a new thread from the mention (ts)
        thread_ts = event.get("thread_ts") or event.get("ts")

        if not user_id or not text:
            return result

        # Check rate limit
        rate_limiter = get_rate_limiter()
        is_allowed, seconds_until_reset = rate_limiter.is_allowed(user_id)

        if not is_allowed:
            # Rate limited - send a gentle message
            try:
                say(
                    text=f"Whoa there! Please wait {seconds_until_reset} seconds before asking me another question.",
                    thread_ts=thread_ts,
                )
            except SlackApiError as e:
                logger.debug(f"MENTION: Could not send rate limit message: {e}")
            result["error"] = "rate_limited"
            return result

        # Remove the bot's own mention from text, preserving user mentions
        # (the tool agent needs <@U...> IDs from the question). Falls back to
        # stripping all mentions when the bot's ID is unknown.
        if bot_user_id:
            clean_text = re.sub(rf"<@{re.escape(bot_user_id)}(\|[^>]+)?>", "", text).strip()
        else:
            clean_text = re.sub(r"<@[A-Z0-9]+(\|[^>]+)?>", "", text).strip()

        if not clean_text:
            # Just a mention with no question - provide help
            clean_text = "help"

        # Classify the question
        question_type = classify_question(clean_text)
        result["question_type"] = question_type

        logger.info(f"MENTION: User {user_id} asked '{clean_text[:50]}...' (type: {question_type})")

        # Generate response
        from services.mention_responder import generate_mention_response

        response = generate_mention_response(
            app=app,
            question_text=clean_text,
            question_type=question_type,
            user_id=user_id,
        )

        if response:
            # Reply in thread (using say() with explicit thread_ts to ensure threading)
            try:
                say(text=response, thread_ts=thread_ts)
                result["responded"] = True
                logger.info(f"MENTION: Responded to {user_id} in {channel}")
            except SlackApiError as e:
                logger.error(f"MENTION: Failed to send response: {e}")
                result["error"] = str(e)
        else:
            result["error"] = "no_response_generated"

    except ImportError as e:
        logger.debug(f"MENTION: Config not available: {e}")
    except Exception as e:
        logger.error(f"MENTION: Error handling mention: {e}")
        result["error"] = str(e)

    return result


def register_mention_handlers(app):
    """
    Register the app_mention event handler.

    Args:
        app: Slack app instance
    """
    try:
        from config import MENTION_QA_ENABLED

        if not MENTION_QA_ENABLED:
            logger.info("MENTION: Q&A is disabled, not registering handler")
            return
    except ImportError:
        logger.debug("MENTION: Config not available, skipping registration")
        return

    @app.event("app_mention")
    def handle_app_mention(event, say, client, context, logger):
        """Handle @-mentions of the bot."""
        result = handle_mention(app, event, say, bot_user_id=context.get("bot_user_id"))

        if result.get("responded"):
            logger.debug(
                f"MENTION: Successfully responded to mention (type: {result.get('question_type')})"
            )
        elif result.get("error"):
            logger.debug(f"MENTION: Failed to respond - {result.get('error')}")

    logger.info("MENTION: Registered app_mention event handler")
