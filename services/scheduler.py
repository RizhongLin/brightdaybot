"""
Background scheduling system for automatic birthday celebrations and cache maintenance.

Manages timezone-aware hourly checks and simple daily announcements in a
separate daemon thread. Supports startup recovery and dynamic reconfiguration.

Key functions:
- setup_scheduler(), run_now(): Scheduler initialization and manual triggers
- hourly_task(), daily_task(): Birthday check tasks
- weekly_calendarific_refresh_task(): Weekly Calendarific cache refresh (Sundays)
- monthly_observances_refresh_task(): Monthly observances cache refresh (1st of month)
  Refreshes UN, UNESCO, and WHO caches.

Uses schedule library and threading for non-blocking execution.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import schedule
from filelock import FileLock

from config import (
    CACHE_REFRESH_TIME,
    CANVAS_DASHBOARD_ENABLED,
    DAILY_CHECK_TIME,
    HEARTBEAT_STALE_THRESHOLD_SECONDS,
    ICS_SUBSCRIPTIONS_ENABLED,
    SCHEDULER_CHECK_INTERVAL_SECONDS,
    SCHEDULER_STATS_FILE,
    SCHEDULER_STATS_SAVE_INTERVAL,
    TIMEOUTS,
    get_logger,
)
from services.birthday import celebrate_missed_birthdays

logger = get_logger("scheduler")

# Store the callback functions and settings
_timezone_aware_callback = None
_simple_daily_callback = None
_app_instance = None
_timezone_enabled = None
_check_interval = None

# Scheduler health monitoring
_scheduler_thread = None
_last_heartbeat = None
_total_executions = 0
_failed_executions = 0
_scheduler_running = False

# Persistence configuration
SCHEDULER_STATS_LOCK_FILE = SCHEDULER_STATS_FILE + ".lock"


def load_scheduler_stats() -> dict:
    """
    Load scheduler statistics from persistent storage.

    Returns:
        dict: Scheduler statistics with keys:
            - total_executions: int
            - failed_executions: int
            - last_heartbeat: ISO timestamp string or None
            - started_at: ISO timestamp of first run
            - last_saved: ISO timestamp of last save
    """
    try:
        lock = FileLock(SCHEDULER_STATS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
        with lock:
            if os.path.exists(SCHEDULER_STATS_FILE):
                with open(SCHEDULER_STATS_FILE, "r") as f:
                    return json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"SCHEDULER_STATS: Failed to load stats: {e}")
    return {
        "total_executions": 0,
        "failed_executions": 0,
        "last_heartbeat": None,
        "started_at": None,
        "last_saved": None,
    }


def save_scheduler_stats(
    total_executions: int, failed_executions: int, last_heartbeat: datetime | None
) -> bool:
    """
    Save scheduler statistics to persistent storage.

    Args:
        total_executions: Total number of scheduler loop executions
        failed_executions: Number of failed executions
        last_heartbeat: Last heartbeat datetime

    Returns:
        bool: True if save successful, False otherwise
    """
    try:
        # Load existing to preserve started_at
        existing = load_scheduler_stats()
        started_at = existing.get("started_at")
        if not started_at:
            started_at = datetime.now(timezone.utc).isoformat()

        data = {
            "total_executions": total_executions,
            "failed_executions": failed_executions,
            "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
            "started_at": started_at,
            "last_saved": datetime.now(timezone.utc).isoformat(),
        }

        lock = FileLock(SCHEDULER_STATS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
        with lock:
            with open(SCHEDULER_STATS_FILE, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
        logger.debug(
            f"SCHEDULER_STATS: Saved stats - {total_executions} total, {failed_executions} failed"
        )
        return True
    except Exception as e:
        logger.error(f"SCHEDULER_STATS: Failed to save stats: {e}")
        return False


def hourly_task():
    """
    Run the hourly birthday check task for timezone-aware celebrations.
    This function is called every hour to check for birthdays in different timezones.
    Checks timezone mode at runtime to allow dynamic mode switching.
    """
    from storage.settings import load_timezone_settings

    # Check if timezone mode is enabled at runtime (allows dynamic switching)
    timezone_enabled, _ = load_timezone_settings()
    if not timezone_enabled:
        logger.debug("SCHEDULER: Timezone mode disabled, skipping hourly check")
        return

    current_time = datetime.now(timezone.utc)
    local_time = datetime.now()
    logger.info(
        f"SCHEDULER: Running hourly timezone-aware check at {local_time.strftime('%H:%M:%S')} local time ({current_time} UTC)"
    )

    if _timezone_aware_callback and _app_instance:
        _timezone_aware_callback(_app_instance, current_time)
    else:
        logger.error("SCHEDULER: No timezone-aware callback or app instance registered")


def daily_task():
    """
    Daily task - runs simple daily check for all birthdays at once.
    Checks timezone mode at runtime to allow dynamic mode switching.
    Only runs when timezone mode is DISABLED.
    """
    from storage.settings import load_timezone_settings

    # Check if timezone mode is disabled at runtime (allows dynamic switching)
    timezone_enabled, _ = load_timezone_settings()
    if timezone_enabled:
        logger.debug("SCHEDULER: Timezone mode enabled, skipping daily check (using hourly)")
        return

    current_time = datetime.now(timezone.utc)
    local_time = datetime.now()
    logger.info(
        f"SCHEDULER: Running simple daily check at {local_time.strftime('%H:%M:%S')} local time ({current_time} UTC)"
    )

    if _simple_daily_callback and _app_instance:
        _simple_daily_callback(_app_instance, current_time)
    else:
        logger.error("SCHEDULER: No simple daily callback or app instance registered")


def weekly_calendarific_refresh_task():
    """Weekly task — refreshes all Calendarific source caches."""
    from config import CALENDARIFIC_ENABLED

    logger.info("SCHEDULER: Running weekly Calendarific cache refresh")

    if CALENDARIFIC_ENABLED:
        try:
            from integrations.calendarific import get_calendarific_client

            results = get_calendarific_client().prefetch_all(force=True)
            logger.info(f"SCHEDULER: Calendarific prefetch complete: {results}")
        except Exception as e:
            logger.error(f"SCHEDULER: Failed to refresh Calendarific cache: {e}")
    else:
        logger.debug("SCHEDULER: Calendarific not enabled, skipping refresh")


def weekly_special_days_task():
    """
    Weekly task - sends special days digest if in weekly mode.
    Runs on the configured day at SPECIAL_DAYS_CHECK_TIME.
    """
    from storage.special_days import get_special_days_mode

    # Only run if in weekly mode
    if get_special_days_mode() != "weekly":
        logger.debug("SCHEDULER: Not in weekly mode, skipping weekly special days task")
        return

    current_time = datetime.now(timezone.utc)
    local_time = datetime.now()
    logger.info(
        f"SCHEDULER: Running weekly special days digest at {local_time.strftime('%H:%M:%S')} local time"
    )

    if _app_instance:
        from services.birthday import check_and_announce_weekly_special_days

        check_and_announce_weekly_special_days(_app_instance, current_time)
    else:
        logger.error("SCHEDULER: No app instance registered for weekly special days")


def monthly_observances_refresh_task():
    """
    Monthly task - refreshes all observances caches (UN, UNESCO, WHO).
    Runs on the 1st of each month at CACHE_REFRESH_TIME.
    Observances data rarely changes, so monthly refresh is sufficient.
    """
    from integrations.observances import get_enabled_sources

    logger.info("SCHEDULER: Running monthly observances cache refresh")

    sources = get_enabled_sources()
    if not sources:
        logger.debug("SCHEDULER: No observance sources enabled, skipping refresh")
        return

    for name, refresh_fn, _status_fn in sources:
        try:
            stats = refresh_fn(force=True)
            logger.info(f"SCHEDULER: {name} observances refresh complete: {stats}")
        except Exception as e:
            logger.error(f"SCHEDULER: Failed to refresh {name} observances cache: {e}")


def daily_ics_refresh_task():
    """Daily task — refreshes all ICS calendar subscription caches."""
    if not ICS_SUBSCRIPTIONS_ENABLED:
        return
    try:
        from integrations.ics_feed import get_ics_feed_client

        results = get_ics_feed_client().refresh_all()
        total = sum(r.get("event_count", 0) for r in results.values() if not r.get("error"))
        logger.info(f"SCHEDULER: ICS refresh complete: {total} events")
    except Exception as e:
        logger.error(f"SCHEDULER: ICS refresh failed: {e}")


def canvas_refresh_task():
    """Periodic task to refresh the ops channel canvas dashboard."""
    from config import CANVAS_DASHBOARD_ENABLED, OPS_CHANNEL_ID

    if not CANVAS_DASHBOARD_ENABLED or not OPS_CHANNEL_ID or not _app_instance:
        return

    try:
        from slack.canvas import update_canvas

        update_canvas(_app_instance, reason="periodic")
    except Exception as e:
        logger.error(f"SCHEDULER: Canvas refresh failed: {e}")


def run_scheduler():
    """Run the scheduler in a separate thread with health monitoring and stats persistence"""
    global _last_heartbeat, _total_executions, _failed_executions, _scheduler_running

    # Load persisted stats on startup
    persisted_stats = load_scheduler_stats()
    _total_executions = persisted_stats.get("total_executions", 0)
    _failed_executions = persisted_stats.get("failed_executions", 0)
    logger.info(
        f"SCHEDULER_HEALTH: Loaded persisted stats - {_total_executions} total, {_failed_executions} failed"
    )

    _scheduler_running = True
    logger.info("SCHEDULER_HEALTH: Scheduler thread started and running")

    last_save_count = _total_executions

    while True:
        try:
            # Update heartbeat
            _last_heartbeat = datetime.now()

            # Run scheduled tasks
            pending_count = len(schedule.jobs)
            if pending_count > 0:
                _total_executions += 1

            schedule.run_pending()

            # Save stats periodically
            if _total_executions - last_save_count >= SCHEDULER_STATS_SAVE_INTERVAL:
                save_scheduler_stats(_total_executions, _failed_executions, _last_heartbeat)
                last_save_count = _total_executions

            time.sleep(SCHEDULER_CHECK_INTERVAL_SECONDS)

        except Exception as e:
            _failed_executions += 1
            logger.error(f"SCHEDULER_HEALTH: Error in scheduler loop: {e}")
            from slack.canvas import safe_record_warning

            safe_record_warning(f"Scheduler error: {e}")
            # Save stats on error to persist failure count
            save_scheduler_stats(_total_executions, _failed_executions, _last_heartbeat)
            time.sleep(SCHEDULER_CHECK_INTERVAL_SECONDS)  # Continue running even after errors


def setup_scheduler(app, timezone_aware_check, simple_daily_check):
    """
    Set up the scheduled tasks

    Args:
        app: Slack app instance
        timezone_aware_check: Function to call for timezone-aware birthday checks
        simple_daily_check: Function to call for simple daily birthday checks
    """
    global _timezone_aware_callback, _simple_daily_callback, _app_instance, _timezone_enabled, _check_interval, _scheduler_thread
    _timezone_aware_callback = timezone_aware_check
    _simple_daily_callback = simple_daily_check
    _app_instance = app

    # Load timezone settings for logging current state
    from storage.settings import load_timezone_settings

    _timezone_enabled, _check_interval = load_timezone_settings()

    # Schedule BOTH hourly and daily tasks - they check mode at runtime
    # This allows dynamic mode switching without bot restart
    schedule.every().hour.at(":00").do(hourly_task)
    schedule.every().day.at(DAILY_CHECK_TIME.strftime("%H:%M")).do(daily_task)

    # Get time zone info for logging
    local_timezone = datetime.now().astimezone().tzinfo

    # Log current mode for visibility
    if _timezone_enabled:
        logger.info(
            "SCHEDULER: Birthday tasks scheduled (current: timezone-aware mode, hourly at :00)"
        )
    else:
        logger.info(
            f"SCHEDULER: Birthday tasks scheduled (current: daily mode at {DAILY_CHECK_TIME.strftime('%H:%M')} {local_timezone})"
        )

    # Schedule weekly Calendarific cache refresh (every Sunday)
    cache_time_str = CACHE_REFRESH_TIME.strftime("%H:%M")
    schedule.every().sunday.at(cache_time_str).do(weekly_calendarific_refresh_task)
    logger.info(f"SCHEDULER: Weekly Calendarific refresh scheduled for Sunday {cache_time_str}")

    # Schedule monthly observances cache refresh (1st of each month)
    # Refreshes UN, UNESCO, and WHO observances caches
    # Using day 1 check - runs daily but only executes on the 1st
    def monthly_observances_check():
        if datetime.now().day == 1:
            monthly_observances_refresh_task()

    schedule.every().day.at(cache_time_str).do(monthly_observances_check)
    logger.info(
        f"SCHEDULER: Monthly observances refresh (UN/UNESCO/WHO) scheduled for 1st of each month at {cache_time_str}"
    )

    # Schedule weekly special days digest task
    # Always schedule this - the task itself checks the mode at runtime
    # This allows mode changes to take effect without bot restart
    from config import SPECIAL_DAYS_CHECK_TIME, WEEKDAY_NAMES
    from storage.special_days import get_special_days_mode, get_weekly_day

    special_time_str = SPECIAL_DAYS_CHECK_TIME.strftime("%H:%M")

    # Schedule for all days - the task checks mode and configured day at runtime
    schedule.every().day.at(special_time_str).do(weekly_special_days_task)

    # Log current mode for visibility
    special_days_mode = get_special_days_mode()
    if special_days_mode == "weekly":
        weekly_day = get_weekly_day()
        day_name = WEEKDAY_NAMES[weekly_day]
        logger.info(
            f"SCHEDULER: Weekly special days task scheduled (current: weekly mode on {day_name.capitalize()})"
        )
    else:
        logger.info(
            "SCHEDULER: Weekly special days task scheduled (current: daily mode, task will skip)"
        )

    # Schedule daily ICS subscription refresh
    if ICS_SUBSCRIPTIONS_ENABLED:
        schedule.every().day.at(cache_time_str).do(daily_ics_refresh_task)
        logger.info(f"SCHEDULER: Daily ICS subscription refresh scheduled at {cache_time_str}")

    # Schedule canvas dashboard refresh
    if CANVAS_DASHBOARD_ENABLED:
        schedule.every().hour.at(":00").do(canvas_refresh_task)
        schedule.every().hour.at(":30").do(canvas_refresh_task)
        logger.info("SCHEDULER: Canvas dashboard refresh scheduled at :00 and :30")

    # Start the scheduler in a separate thread
    _scheduler_thread = threading.Thread(target=run_scheduler)
    _scheduler_thread.daemon = True  # Make thread exit when main program exits
    _scheduler_thread.start()
    logger.info("SCHEDULER: Background scheduler thread started")


def run_now():
    """Run the appropriate birthday check immediately and perform startup catch-up"""
    if not _app_instance:
        logger.error("SCHEDULER: No app instance registered")
        return

    current_time = datetime.now(timezone.utc)
    local_time = datetime.now()
    logger.info(
        f"SCHEDULER: Running startup birthday check at {local_time.strftime('%H:%M:%S')} local time ({current_time} UTC)"
    )

    # Run startup catch-up
    startup_birthday_catchup(_app_instance, current_time)

    # Then run appropriate check based on stored timezone settings
    # This covers: user birthdays, bot birthday, and daily special days
    if _timezone_enabled and _timezone_aware_callback:
        _timezone_aware_callback(_app_instance, current_time)
    elif not _timezone_enabled and _simple_daily_callback:
        _simple_daily_callback(_app_instance, current_time)
    else:
        logger.error("SCHEDULER: No appropriate callback registered for current mode")

    # Catch up weekly special day digest if missed (has its own day/time/dedup guards)
    from services.birthday import check_and_announce_weekly_special_days

    check_and_announce_weekly_special_days(_app_instance, current_time)


def startup_birthday_catchup(app, current_time):
    """
    Check for missed birthday celebrations due to server downtime.

    This function simply delegates to celebrate_missed_birthdays() which handles
    all the logic for finding and celebrating birthdays that should have been
    announced today but weren't due to system downtime.

    Args:
        app: Slack app instance
        current_time: Current datetime (for logging)
    """
    logger.info(
        f"STARTUP: Checking for missed birthday celebrations due to server downtime at {current_time}"
    )

    # Celebrate uncelebrated birthdays for today (respects announcement timing)
    celebrate_missed_birthdays(app)


def get_scheduler_health():
    """
    Get scheduler health status for monitoring

    Returns:
        dict: Scheduler health information including persisted stats
    """
    global _scheduler_thread, _last_heartbeat, _total_executions, _failed_executions, _scheduler_running

    now = datetime.now()

    # Check if thread is alive
    thread_alive = _scheduler_thread is not None and _scheduler_thread.is_alive()

    # Check heartbeat freshness (should be within threshold)
    heartbeat_fresh = False
    heartbeat_age_seconds = None
    if _last_heartbeat:
        heartbeat_age = now - _last_heartbeat
        heartbeat_age_seconds = heartbeat_age.total_seconds()
        heartbeat_fresh = heartbeat_age_seconds < HEARTBEAT_STALE_THRESHOLD_SECONDS

    # Calculate success rate
    success_rate = None
    if _total_executions > 0:
        success_rate = ((_total_executions - _failed_executions) / _total_executions) * 100

    health_status = "ok" if (thread_alive and heartbeat_fresh and _scheduler_running) else "error"

    # Get persisted stats for additional info
    persisted_stats = load_scheduler_stats()

    return {
        "status": health_status,
        "thread_alive": thread_alive,
        "scheduler_running": _scheduler_running,
        "last_heartbeat": _last_heartbeat.isoformat() if _last_heartbeat else None,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "heartbeat_fresh": heartbeat_fresh,
        "total_executions": _total_executions,
        "failed_executions": _failed_executions,
        "success_rate_percent": success_rate,
        "scheduled_jobs": len(schedule.jobs),
        "timezone_enabled": _timezone_enabled,
        "check_interval_hours": _check_interval,
        "started_at": persisted_stats.get("started_at"),
        "last_saved": persisted_stats.get("last_saved"),
    }


def watchdog_should_exit(health, uptime_seconds, grace_seconds=300):
    """
    Decide whether the watchdog should terminate the process.

    Pure decision function (no side effects) so it can be unit-tested.

    Args:
        health: dict from get_scheduler_health()
        uptime_seconds: Seconds since the watchdog started
        grace_seconds: Startup grace period during which we never exit
                       (startup catch-up can legitimately block the loop)

    Returns:
        bool: True if the process should exit so systemd restarts it
    """
    if uptime_seconds < grace_seconds:
        return False

    if not health.get("thread_alive", False):
        return True

    # A hung loop keeps the thread alive but stops heartbeating. Use a hard
    # threshold well beyond the "stale" warning level to avoid false exits.
    age = health.get("heartbeat_age_seconds")
    if age is not None and age > 10 * HEARTBEAT_STALE_THRESHOLD_SECONDS:
        return True

    return False


def start_scheduler_watchdog(grace_seconds=300, interval_seconds=60):
    """
    Start a daemon thread that exits the process if the scheduler dies.

    The scheduler runs in a daemon thread with no in-process restart; canvas
    refresh and health reporting run *inside* that thread, so a dead scheduler
    is invisible from within. This watchdog runs outside it and forces a
    process exit, letting systemd (Restart=on-failure) bring the bot back.
    """
    started = time.monotonic()

    def _watch():
        while True:
            time.sleep(interval_seconds)
            try:
                health = get_scheduler_health()
                if watchdog_should_exit(health, time.monotonic() - started, grace_seconds):
                    logger.critical(
                        "WATCHDOG: Scheduler thread dead or heartbeat stalled "
                        f"(thread_alive={health.get('thread_alive')}, "
                        f"heartbeat_age={health.get('heartbeat_age_seconds')}s) — "
                        "exiting so systemd restarts the bot"
                    )
                    try:
                        from slack.canvas import safe_record_warning

                        safe_record_warning(
                            "Scheduler watchdog triggered a restart (thread dead or hung)"
                        )
                    except Exception:
                        pass
                    os._exit(1)
            except Exception as e:
                logger.error(f"WATCHDOG: Health check failed (non-fatal): {e}")

    watchdog = threading.Thread(target=_watch, name="scheduler-watchdog", daemon=True)
    watchdog.start()
    logger.info(
        f"WATCHDOG: Scheduler watchdog started (interval={interval_seconds}s, "
        f"grace={grace_seconds}s)"
    )


def get_scheduler_summary():
    """
    Get human-readable scheduler health summary

    Returns:
        str: Human-readable scheduler status
    """
    health = get_scheduler_health()

    if health["status"] == "ok":
        return f"✅ Scheduler healthy - {health['scheduled_jobs']} jobs, {health['success_rate_percent']:.1f}% success rate"
    else:
        issues = []
        if not health["thread_alive"]:
            issues.append("thread not running")
        if not health["heartbeat_fresh"]:
            age = health["heartbeat_age_seconds"]
            if age is not None:
                issues.append(f"heartbeat stale ({age:.0f}s ago)")
            else:
                issues.append("no heartbeat recorded")
        if not health["scheduler_running"]:
            issues.append("not initialized")

        return f"❌ Scheduler issues: {', '.join(issues)}"
