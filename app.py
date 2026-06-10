"""
BrightDayBot - AI-Powered Slack Birthday Celebration Bot

Main application entry point that initializes Slack Bot framework, event handlers,
and background scheduling for automatic birthday celebrations.

Features: AI messages/images, timezone-aware celebrations, multiple personalities,
admin system, automatic backups, component-specific logging.
Uses Slack Bolt, OpenAI API, and background scheduling.
"""

import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Import configuration
from config import logger
from handlers.app_home_handler import register_app_home_handlers

# Import event handlers
from handlers.event_handler import register_event_handlers
from handlers.mention_handler import register_mention_handlers
from handlers.modal_handler import register_modal_handlers
from handlers.slash_handler import register_slash_commands
from services.birthday import simple_daily_check, timezone_aware_check

# Import services
from services.scheduler import run_now, setup_scheduler, start_scheduler_watchdog
from storage.settings import initialize_config
from storage.special_days import initialize_special_days_cache
from utils.health import get_missing_required_env

# Initialize configuration from storage files
initialize_config()

# Fail fast on missing secrets when running as the main process. Must happen
# before App() so the operator sees a clear message instead of an SDK
# traceback at the first API call. Guarded so plain imports (tests, CI import
# checks) stay side-effect free.
if __name__ == "__main__":
    _missing_env = get_missing_required_env()
    if _missing_env:
        logger.critical(
            f"STARTUP: Missing required environment variables: {', '.join(_missing_env)}. "
            "Set them in .env before starting the bot."
        )
        sys.exit(1)

# Initialize Slack app with error handling
app = App()
logger.info("INIT: App initialized")

# Register event handlers
register_event_handlers(app)
register_mention_handlers(app)

# Register slash commands and interactive components
register_slash_commands(app)
register_modal_handlers(app)
register_app_home_handlers(app)


def _check_deploy_notification(app):
    """Detect new deploy on startup and trigger canvas refresh."""
    try:
        import json
        import os

        from config import CANVAS_SETTINGS_FILE, DEPLOY_INFO_FILE
        from slack.canvas import record_change, update_canvas_async

        if not os.path.exists(DEPLOY_INFO_FILE):
            return

        with open(DEPLOY_INFO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Support both old single-object and new array format
        if isinstance(data, list) and data:
            info = data[-1]  # Latest entry
        elif isinstance(data, dict):
            info = data
        else:
            return

        new_commit = info.get("new_short", "")
        if not new_commit:
            return

        # Check if we already processed this deploy
        settings = {}
        if os.path.exists(CANVAS_SETTINGS_FILE):
            with open(CANVAS_SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)

        if settings.get("last_deploy_commit") == new_commit:
            return

        old_short = info.get("old_short", "?")
        status = info.get("status", "success")
        record_change(f"Deploy: `{old_short}` → `{new_commit}` ({status})")

        settings["last_deploy_commit"] = new_commit
        with open(CANVAS_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

        update_canvas_async(app, reason="deploy")
        logger.info(f"INIT: Deploy detected ({old_short} → {new_commit}), canvas refresh triggered")

    except Exception as e:
        logger.warning(f"INIT: Deploy detection failed (non-fatal): {e}")


# Start the app
if __name__ == "__main__":
    handler = SocketModeHandler(app)
    logger.info("INIT: Handler initialized, starting app")
    try:
        # Set up the scheduler with direct birthday check functions
        setup_scheduler(app, timezone_aware_check, simple_daily_check)

        # Watchdog: exit the process (systemd restarts us) if the scheduler
        # thread dies or its heartbeat stalls — it has no in-process restart
        start_scheduler_watchdog()

        # Initialize special days caches if stale or missing
        initialize_special_days_cache()

        # Detect new deploy and trigger canvas refresh
        _check_deploy_notification(app)

        # Check for today's birthdays at startup and catch up on missed celebrations
        run_now()

        # Start the app
        handler.start()
    except Exception as e:
        logger.critical(f"CRITICAL: Error starting app: {e}")
