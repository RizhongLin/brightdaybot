"""
Slack Canvas dashboard for ops channel.

Maintains a living Canvas document with birthday data summary,
system health, scheduler status, and observance cache status.
"""

import collections
import json
import os
import threading
from datetime import datetime

from slack_sdk.errors import SlackApiError

from config import (
    CANVAS_DASHBOARD_ENABLED,
    CANVAS_DEPLOY_DISPLAY_MAX,
    CANVAS_MIN_UPDATE_INTERVAL_SECONDS,
    CANVAS_RECENT_CHANGES_MAX,
    CANVAS_SETTINGS_FILE,
    CANVAS_WARNINGS_MAX,
    CANVAS_WARNINGS_TTL_HOURS,
    DEPLOY_INFO_FILE,
    ICS_SUBSCRIPTIONS_ENABLED,
    OPS_CHANNEL_ID,
    SLACK_HISTORY_PAGE_SIZE,
    get_logger,
)
from slack.messaging import send_message

logger = get_logger("slack")


def _flag(val):
    """Format a boolean as ✅/❌ for dashboard display."""
    return "✅" if val else "❌"


# Module state
_recent_changes = collections.deque(maxlen=CANVAS_RECENT_CHANGES_MAX)
_recent_warnings = collections.deque(maxlen=CANVAS_WARNINGS_MAX)
_warnings_ttl_seconds = CANVAS_WARNINGS_TTL_HOURS * 3600
_last_update_time = None
_last_sched_ok = None
_last_sd_total = None
_update_lock = threading.Lock()


# --- Canvas ID persistence ---


def _load_settings():
    """Load canvas settings from file."""
    try:
        if os.path.exists(CANVAS_SETTINGS_FILE):
            with open(CANVAS_SETTINGS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"CANVAS: Failed to load settings: {e}")
    return {}


def _save_settings(updates):
    """Merge updates into canvas settings file."""
    try:
        data = _load_settings()
        data.update(updates)
        with open(CANVAS_SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"CANVAS: Failed to save settings: {e}")
        return False


def _load_canvas_id():
    """Load stored canvas ID from settings file."""
    return _load_settings().get("canvas_id")


def _save_canvas_id(canvas_id):
    """Save or clear canvas ID in settings file."""
    if canvas_id:
        _save_settings({"canvas_id": canvas_id})
        logger.info(f"CANVAS: Saved canvas ID: {canvas_id}")
    else:
        _clear_setting("canvas_id")
        logger.info("CANVAS: Cleared canvas ID")


def _clear_setting(key):
    """Remove a key from settings file."""
    try:
        data = _load_settings()
        data.pop(key, None)
        with open(CANVAS_SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"CANVAS: Failed to clear setting {key}: {e}")


# --- Canvas lifecycle ---


def _set_canvas_read_only(app, canvas_id, channel_id):
    """Set canvas to read-only for channel members.

    The bot retains write access as the canvas owner.
    """
    try:
        app.client.api_call(
            "canvases.access.set",
            json={
                "canvas_id": canvas_id,
                "access_level": "read",
                "channel_ids": [channel_id],
            },
        )
        logger.info(f"CANVAS: Set canvas {canvas_id} to read-only for channel")
    except SlackApiError as e:
        logger.warning(f"CANVAS: Could not set read-only access: {e}")


def _ensure_canvas(app, channel_id):
    """
    Get existing or create new channel canvas.

    Returns canvas_id or None on failure.
    """
    # Try stored ID first — no validation call needed;
    # update_canvas() handles canvas_not_found and clears the stored ID
    canvas_id = _load_canvas_id()
    if canvas_id:
        return canvas_id

    # Try to create a new channel canvas
    try:
        response = app.client.conversations_canvases_create(
            channel_id=channel_id,
            document_content={
                "type": "markdown",
                "markdown": "# 🤖 BrightDayBot Ops\n\n*Initializing...*",
            },
        )
        canvas_id = response.get("canvas_id")
        if canvas_id:
            _save_canvas_id(canvas_id)
            _set_canvas_read_only(app, canvas_id, channel_id)
            _update_channel_topic(app, channel_id)
            logger.info(f"CANVAS: Created new channel canvas: {canvas_id}")
            return canvas_id
    except SlackApiError as e:
        error_code = e.response.get("error", "")
        if error_code == "channel_canvas_already_exists":
            # Canvas exists but we don't have the ID — retrieve it
            try:
                info = app.client.conversations_info(channel=channel_id)
                canvas_id = (
                    info.get("channel", {}).get("properties", {}).get("canvas", {}).get("id")
                )
                if canvas_id:
                    _save_canvas_id(canvas_id)
                    _set_canvas_read_only(app, canvas_id, channel_id)
                    logger.info(f"CANVAS: Found existing channel canvas: {canvas_id}")
                    return canvas_id
            except SlackApiError as e2:
                logger.error(f"CANVAS: Failed to retrieve existing canvas ID: {e2}")
        elif error_code == "missing_scope":
            logger.error(
                "CANVAS: Bot token missing 'canvases:write' scope. "
                "Add it in Slack app settings at https://api.slack.com/apps"
            )
        else:
            logger.error(f"CANVAS: Failed to create canvas: {e}")

    return None


def _update_channel_topic(app, channel_id, sd_total=None):
    """Update channel topic only when data changes, to avoid system message spam."""
    try:
        from config import BIRTHDAY_CHANNEL
        from services.scheduler import get_scheduler_health
        from slack.client import get_channel_members
        from storage.birthdays import load_birthdays
        from storage.special_days import load_all_special_days

        birthdays = load_birthdays()
        total = len(birthdays)

        # Validate against channel membership
        channel_member_set = set()
        if BIRTHDAY_CHANNEL:
            members = get_channel_members(app, BIRTHDAY_CHANNEL)
            if members:
                channel_member_set = set(members)

        active = sum(
            1
            for uid, b in birthdays.items()
            if (uid in channel_member_set if channel_member_set else True)
            and b.get("preferences", {}).get("active", True)
        )

        health = get_scheduler_health()
        sched_ok = health.get("status") == "ok"

        # Total special days count (use precomputed if available)
        if sd_total is None:
            sd_total = len(load_all_special_days())

        global _last_sched_ok

        # Data fingerprints for change detection (emoji-free to avoid
        # Slack's Unicode → :emoji: conversion mismatch on readback)
        data_fingerprint = f"{active}/{total} active"
        sd_fingerprint = f"{sd_total} special days"

        topic = (
            f"🤖 BrightDayBot Ops | "
            f"🎂 {data_fingerprint} | "
            f"🌍 {sd_fingerprint} | "
            f"⚙️ {'✅' if sched_ok else '⚠️'}"
        )

        # Track scheduler status changes in memory (can't use emoji in
        # topic comparison — Slack converts Unicode to :emoji: shortcodes)
        sched_changed = _last_sched_ok is not None and _last_sched_ok != sched_ok
        _last_sched_ok = sched_ok

        # Read current topic/purpose to decide whether to update
        try:
            info = app.client.conversations_info(channel=channel_id)
            channel_data = info.get("channel", {})
            current_topic = channel_data.get("topic", {}).get("value", "")
            current_purpose = channel_data.get("purpose", {}).get("value", "")
        except SlackApiError:
            current_topic = ""
            current_purpose = ""

        # Compare on data content only — Slack converts Unicode emojis to
        # :emoji: format so direct string comparison always fails.
        topic_data_unchanged = (
            "BrightDayBot Ops" in current_topic
            and data_fingerprint in current_topic
            and sd_fingerprint in current_topic
            and not sched_changed
        )

        if not topic_data_unchanged:
            if not current_topic or "BrightDayBot Ops" in current_topic:
                app.client.conversations_setTopic(channel=channel_id, topic=topic)
                logger.info(f"CANVAS: Updated channel topic for {channel_id}")
            else:
                logger.info(
                    f"CANVAS: Skipping topic update — manually set by user: {current_topic!r}"
                )

        # Set purpose if empty or ours
        expected_purpose = "BrightDayBot ops hub — canvas dashboard auto-refreshes every half hour with system health, birthday data, scheduler, caches, and backups."
        if not current_purpose or "BrightDayBot ops hub" in current_purpose:
            if current_purpose != expected_purpose:
                try:
                    app.client.conversations_setPurpose(
                        channel=channel_id, purpose=expected_purpose
                    )
                    logger.info(f"CANVAS: Updated channel purpose for {channel_id}")
                except SlackApiError as e:
                    logger.debug(f"CANVAS: Could not update channel purpose: {e}")
    except SlackApiError as e:
        logger.warning(f"CANVAS: Could not update channel topic: {e}")
    except Exception as e:
        logger.warning(f"CANVAS: Error building channel topic: {e}")


# --- Dashboard content ---


def _format_deploy_entry(info):
    """Format a single deploy entry for canvas display."""
    old_short = info.get("old_short", "?")
    new_short = info.get("new_short", "?")
    timestamp = info.get("timestamp", "?")
    duration = info.get("duration_seconds", "?")
    status = info.get("status", "success")
    commits = info.get("commits", [])

    # Format timestamp for display (strip timezone offset)
    if isinstance(timestamp, str) and "T" in timestamp:
        timestamp = timestamp.replace("T", " ")[:19]

    status_emoji = {"success": "✅", "rolled_back": "⚠️", "failed": "❌", "build_failed": "❌"}.get(
        status, "❓"
    )

    line = f"`{old_short}` → `{new_short}` {status_emoji} `{timestamp}` ({duration}s)"
    if commits:
        line += " — " + ", ".join(
            c.split(" ", 1)[1] if " " in c else c for c in commits[:CANVAS_DEPLOY_DISPLAY_MAX]
        )
    return line


def _build_deploy_section():
    """Build deploy history section from deploy_info.json (shows latest 3)."""
    try:
        if not os.path.exists(DEPLOY_INFO_FILE):
            return None

        with open(DEPLOY_INFO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Support both old single-object and new array format
        if isinstance(data, dict):
            deploys = [data]
        elif isinstance(data, list):
            deploys = data
        else:
            return None

        if not deploys:
            return None

        # Show latest 3, newest first
        recent = deploys[-CANVAS_DEPLOY_DISPLAY_MAX:][::-1]
        lines = [f"## 🚀 Deploys ({len(deploys)} total)"]
        for entry in recent:
            lines.append(f"- {_format_deploy_entry(entry)}")

        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"CANVAS: Could not build deploy section: {e}")
        return None


def _build_dashboard_markdown(app=None):
    """Build the full dashboard markdown from existing data sources."""
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    warnings_section = _build_warnings_section()
    deploy_section = _build_deploy_section()
    sections = [
        f"## 🕐 Last refreshed: `{timestamp}`",
        _build_birthday_section(app),
        _build_health_section(),
    ]
    if deploy_section:
        sections.append(deploy_section)
    if warnings_section:
        sections.append(warnings_section)
    sections += [
        _build_engagement_section(),
        _build_scheduler_section(),
        _build_ai_usage_section(),
        _build_observances_section(),
        _build_backups_section(app),
    ]
    sections.append("*🔄 Auto-updates on birthday changes and every half hour.*")

    return "\n\n---\n\n".join(sections)


def _build_birthday_section(app=None):
    """Build birthday data summary section with channel-validated counts."""
    try:
        from config import BIRTHDAY_CHANNEL
        from slack.client import get_channel_members
        from storage.birthdays import load_birthdays

        birthdays = load_birthdays()
        total = len(birthdays)

        # Cross-reference with actual channel members
        channel_member_set = set()
        if app and BIRTHDAY_CHANNEL:
            members = get_channel_members(app, BIRTHDAY_CHANNEL)
            if members:
                channel_member_set = set(members)

        # Count only users who are in the channel AND have active preference
        in_channel = 0
        active = 0
        paused = 0
        with_year = 0
        styles = {}
        for user_id, b in birthdays.items():
            is_member = user_id in channel_member_set if channel_member_set else True
            is_active = b.get("preferences", {}).get("active", True)

            if is_member:
                in_channel += 1
                if is_active:
                    active += 1
                else:
                    paused += 1
                if b.get("year"):
                    with_year += 1
                style = b.get("preferences", {}).get("celebration_style", "standard")
                styles[style] = styles.get(style, 0) + 1

        not_in_channel = total - in_channel
        year_pct = round(with_year / in_channel * 100) if in_channel else 0
        style_parts = [f"{s.title()}: {c}" for s, c in sorted(styles.items())]

        # Monthly distribution
        months = [0] * 12
        month_names = [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        for b in birthdays.values():
            date_str = b.get("date", "")
            if "/" in date_str:
                try:
                    month = int(date_str.split("/")[1]) - 1
                    months[month] += 1
                except (ValueError, IndexError):
                    logger.debug(f"CANVAS: Skipped birthday with invalid date format: {date_str}")

        month_parts = [f"{month_names[i]}: {months[i]}" for i in range(12) if months[i] > 0]
        month_line = " · ".join(month_parts) if month_parts else "No data"

        # Recent changes
        changes_text = ""
        changes_snapshot = list(_recent_changes)
        if changes_snapshot:
            changes_lines = "\n".join(f"- {c}" for c in reversed(changes_snapshot))
            changes_text = f"\n\n### 📝 Recent Changes\n{changes_lines}"

        return f"""## 🎂 Birthday Data
| Metric | Value |
|--------|-------|
| 👥 Registered | {total} |
| 📢 In channel | {in_channel} |
| ✅ Active | {active} |
| ⏸️ Paused | {paused} |
| 🚪 Left channel | {not_in_channel} |
| 📅 With birth year | {with_year} ({year_pct}%) |

**🎊 Styles:** {' · '.join(style_parts)}

**📊 Monthly:** {month_line}{changes_text}"""

    except Exception as e:
        logger.error(f"CANVAS: Failed to build birthday section: {e}")
        return "## 🎂 Birthday Data\n*Error loading birthday data.*"


def _build_health_section():
    """Build system health section."""
    try:
        from utils.health import get_system_status

        status = get_system_status(app=None, include_live_checks=False)
        overall = status.get("overall", "unknown")
        components = status.get("components", {})

        from storage.settings import get_current_openai_model, get_current_personality_name

        personality = get_current_personality_name()
        model = get_current_openai_model()

        # Admin count
        admin_count = components.get("admins", {}).get("admin_count", "?")

        # Log sizes
        log_info = components.get("logs", {})
        total_log_mb = log_info.get("total_size_mb", "?")

        # Birthday channel
        channel_info = components.get("birthday_channel", {})
        channel_status = "configured" if channel_info.get("status") == "ok" else "not set"

        # Timezone mode
        from storage.settings import load_timezone_settings

        tz_enabled, _tz_interval = load_timezone_settings()
        tz_mode = "Per-user timezone" if tz_enabled else "Server time"

        # Feature flags
        from config import (
            AI_IMAGE_GENERATION_ENABLED,
            EXTERNAL_BACKUP_ENABLED,
            IMAGE_GENERATION_PARAMS,
            MENTION_QA_ENABLED,
            NLP_DATE_PARSING_ENABLED,
            PROFILE_ANALYSIS_ENABLED,
            SPECIAL_DAYS_IMAGE_ENABLED,
            THREAD_ENGAGEMENT_ENABLED,
            USE_CUSTOM_EMOJIS,
            WEB_SEARCH_CACHE_ENABLED,
        )
        from storage.settings import (
            get_configured_openai_image_model,
            load_bot_celebration_setting,
        )

        active_image_model = get_configured_openai_image_model()

        bot_celebration = load_bot_celebration_setting()

        overall_emoji = "✅" if overall == "ok" else "⚠️"

        img_quality = IMAGE_GENERATION_PARAMS["quality"]["default"]
        img_size = IMAGE_GENERATION_PARAMS["size"]["default"]

        return f"""## 🏥 System Health
- **Status:** {overall_emoji} {overall.title()}
- **Birthday Channel:** {channel_status}
- **Admins:** {admin_count}
- **Personality:** `{personality}` · **Timezone:** {tz_mode}
- **Model:** `{model}` · **Image:** `{active_image_model}` ({img_quality}, {img_size})
- **Logs:** {total_log_mb} MB total

**🔧 Features:** {_flag(THREAD_ENGAGEMENT_ENABLED)} Threads · {_flag(MENTION_QA_ENABLED)} @-Mentions · {_flag(NLP_DATE_PARSING_ENABLED)} NLP dates · {_flag(AI_IMAGE_GENERATION_ENABLED)} AI images · {_flag(SPECIAL_DAYS_IMAGE_ENABLED)} SD images · {_flag(PROFILE_ANALYSIS_ENABLED)} Profiles · {_flag(WEB_SEARCH_CACHE_ENABLED)} Web cache · {_flag(USE_CUSTOM_EMOJIS)} Custom emoji · {_flag(bot_celebration)} Bot birthday · {_flag(EXTERNAL_BACKUP_ENABLED)} Ext. backups"""

    except Exception as e:
        logger.error(f"CANVAS: Failed to build health section: {e}")
        return "## 🏥 System Health\n*Error loading health data.*"


def _build_engagement_section():
    """Build thread engagement stats section."""
    try:
        from storage.thread_tracking import get_thread_tracker

        tracker = get_thread_tracker()
        stats = tracker.get_all_stats()
        active = stats.get("active_threads", 0)
        total_tracked = stats.get("total_tracked", 0)
        reactions = stats.get("total_reactions", 0)

        return f"""## 💬 Thread Engagement
- **Active threads:** {active} (of {total_tracked} tracked)
- **Total reactions:** {reactions}"""

    except Exception as e:
        logger.error(f"CANVAS: Failed to build engagement section: {e}")
        return "## 💬 Thread Engagement\n*Error loading engagement data.*"


def _build_scheduler_section():
    """Build scheduler status section."""
    try:
        from services.scheduler import get_scheduler_health

        health = get_scheduler_health()
        status = health.get("status", "unknown")
        thread_alive = health.get("thread_alive", False)
        heartbeat_age = health.get("heartbeat_age_seconds", "?")
        jobs = health.get("scheduled_jobs", "?")
        success_rate_raw = health.get("success_rate_percent")
        success_rate = (
            f"{success_rate_raw:.1f}" if isinstance(success_rate_raw, (int, float)) else "?"
        )
        total = health.get("total_executions", 0)
        failed = health.get("failed_executions", 0)
        started_raw = health.get("started_at", "?")
        started = started_raw
        if isinstance(started, str) and "T" in started:
            started = started.replace("T", " ")[:19]

        # Calculate uptime
        uptime_text = ""
        if isinstance(started_raw, str):
            try:
                started_dt = datetime.fromisoformat(started_raw)
                delta = datetime.now(started_dt.tzinfo) - started_dt
                days = delta.days
                hours = delta.seconds // 3600
                if days > 0:
                    uptime_text = f" ({days}d {hours}h)"
                else:
                    uptime_text = f" ({hours}h)"
            except (ValueError, TypeError):
                pass

        alive_emoji = "🟢" if thread_alive else "🔴"
        heartbeat_text = (
            f"{round(heartbeat_age)}s ago" if isinstance(heartbeat_age, (int, float)) else "?"
        )

        from config import DAILY_CHECK_TIME, SPECIAL_DAYS_CHECK_TIME, TIMEZONE_CELEBRATION_TIME
        from storage.settings import load_timezone_settings

        tz_enabled, _ = load_timezone_settings()
        if tz_enabled:
            timing_line = f"- **Timing:** Birthdays at `{TIMEZONE_CELEBRATION_TIME.strftime('%H:%M')}` (per-user tz) · Special days at `{SPECIAL_DAYS_CHECK_TIME.strftime('%H:%M')}`"
        else:
            timing_line = f"- **Timing:** Birthdays at `{DAILY_CHECK_TIME.strftime('%H:%M')}` · Special days at `{SPECIAL_DAYS_CHECK_TIME.strftime('%H:%M')}` (server time)"

        return f"""## ⏰ Scheduler
- **Status:** {alive_emoji} {status.title()} ({heartbeat_text})
- **Jobs:** {jobs} · **Success rate:** {success_rate}%
- **Executions:** {total} total · {failed} failed
{timing_line}
- **Started:** `{started}`{uptime_text}"""

    except Exception as e:
        logger.error(f"CANVAS: Failed to build scheduler section: {e}")
        return "## ⏰ Scheduler\n*Error loading scheduler data.*"


def _build_ai_usage_section():
    """Build the AI usage section (today's calls and tokens, top contexts)."""
    try:
        from storage.ai_usage import get_today_usage

        usage = get_today_usage()
        if not usage.get("calls"):
            return "## 🤖 AI Usage Today\n*No AI calls yet today.*"

        top_contexts = sorted(
            usage.get("by_context", {}).items(),
            key=lambda kv: kv[1].get("calls", 0),
            reverse=True,
        )[:3]
        top_lines = "\n".join(
            f"- `{name}`: {data.get('calls', 0)} call(s), "
            f"{data.get('input_tokens', 0) + data.get('output_tokens', 0):,} tokens"
            for name, data in top_contexts
        )

        return f"""## 🤖 AI Usage Today
- **API calls**: {usage["calls"]}
- **Tokens**: {usage["input_tokens"]:,} in / {usage["output_tokens"]:,} out

### Top contexts
{top_lines}"""
    except Exception as e:
        logger.error(f"CANVAS: Failed to build AI usage section: {e}")
        return "## 🤖 AI Usage Today\n*Error loading usage data.*"


def _build_observances_section():
    """Build observance caches status section."""
    try:
        from integrations.observances import get_enabled_sources

        sources = get_enabled_sources()
        if not sources:
            return "## Observance Caches\n*No observance sources enabled.*"

        rows = []
        for name, _refresh_fn, status_fn in sources:
            try:
                s = status_fn()
                fresh = "🟢 Fresh" if s.get("cache_fresh") else "🟡 Stale"
                count = s.get("observance_count", "?")
                updated = s.get("last_updated", "?")
                if isinstance(updated, str) and "T" in updated:
                    updated = updated[:10]
                rows.append(f"| {name} | {fresh} | {count} | {updated} |")
            except Exception:
                rows.append(f"| {name} | 🔴 Error | ? | ? |")

        # Calendarific (parent row + indented sub-rows)
        try:
            from config import CALENDARIFIC_ENABLED

            if CALENDARIFIC_ENABLED:
                from integrations.calendarific import get_calendarific_client

                cal_status = get_calendarific_client().get_api_status()
                cal_total = cal_status.get("holiday_count", 0)
                cal_fresh = "🟢 Fresh" if not cal_status.get("needs_prefetch") else "🟡 Stale"
                cal_updated = cal_status.get("last_prefetch", "?")
                if isinstance(cal_updated, str) and "T" in cal_updated:
                    cal_updated = cal_updated[:10]

                rows.append(f"| Calendarific | {cal_fresh} | {cal_total} | {cal_updated} |")
                for sid, info in cal_status.get("sources", {}).items():
                    if info.get("enabled"):
                        count = info.get("holiday_count", "?")
                        rows.append(f"| *↳ {info['label']} ({info['country']})* | | {count} | |")
        except Exception as e:
            logger.debug(f"CANVAS: Could not get Calendarific status: {e}")

        # ICS calendar subscriptions
        try:
            if ICS_SUBSCRIPTIONS_ENABLED:
                from integrations.ics_feed import get_ics_feed_client

                ics_status = get_ics_feed_client().get_status()
                if ics_status.get("enabled_count", 0) > 0:
                    ics_total = ics_status.get("total_events", 0)
                    ics_fresh = "🟢 Fresh" if ics_status.get("all_fresh") else "🟡 Stale"
                    ics_updated = ics_status.get("last_updated", "—")
                    if isinstance(ics_updated, str) and "T" in ics_updated:
                        ics_updated = ics_updated[:10]
                    rows.append(
                        f"| ICS Feeds ({ics_status['enabled_count']}) | {ics_fresh} | {ics_total} | {ics_updated} |"
                    )
        except Exception as e:
            logger.debug(f"CANVAS: Could not get ICS status: {e}")

        # Custom/CSV days
        try:
            from storage.special_days import load_special_days

            custom_days = load_special_days()
            custom_count = len(custom_days)
            rows.append(f"| Custom | — | {custom_count} | — |")
        except Exception as e:
            logger.debug(f"CANVAS: Could not load custom special days: {e}")

        # Total merged count (all sources, deduplicated)
        global _last_sd_total
        try:
            from storage.special_days import load_all_special_days

            total_count = len(load_all_special_days())
            _last_sd_total = total_count
            rows.append(f"| **Total (deduplicated)** | | **{total_count}** | |")
        except Exception as e:
            logger.debug(f"CANVAS: Could not load deduplicated special days count: {e}")

        table_rows = "\n".join(rows)

        # Special day announcement config
        from config import (
            SPECIAL_DAY_MENTION_ENABLED,
            SPECIAL_DAY_THREAD_ENABLED,
            SPECIAL_DAY_TOPIC_UPDATE_ENABLED,
            SPECIAL_DAYS_MODE,
            SPECIAL_DAYS_WEEKLY_DAY,
        )

        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        mode_text = SPECIAL_DAYS_MODE.title()
        if SPECIAL_DAYS_MODE == "weekly":
            mode_text += f" ({day_names[SPECIAL_DAYS_WEEKLY_DAY]})"

        config_line = f"\n\n**📋 Config:** Mode: {mode_text} · @-here: {_flag(SPECIAL_DAY_MENTION_ENABLED)} · Topic update: {_flag(SPECIAL_DAY_TOPIC_UPDATE_ENABLED)} · Thread replies: {_flag(SPECIAL_DAY_THREAD_ENABLED)}"

        return f"""## 🌍 Observance Caches
| Source | Status | Count | Last Updated |
|--------|--------|-------|-------------|
{table_rows}{config_line}"""

    except Exception as e:
        logger.error(f"CANVAS: Failed to build observances section: {e}")
        return "## 🌍 Observance Caches\n*Error loading observance data.*"


def _build_backups_section(app=None):
    """Build backups status section with uploaded file embed."""
    try:
        from config import BACKUP_DIR

        if not os.path.exists(BACKUP_DIR):
            return "## 💾 Backups\n*No backups directory found.*"

        backup_files = sorted(
            [
                os.path.join(BACKUP_DIR, f)
                for f in os.listdir(BACKUP_DIR)
                if f.startswith("birthdays_") and f.endswith(".json")
            ],
            key=lambda x: os.path.getmtime(x),
            reverse=True,
        )

        count = len(backup_files)
        if count == 0:
            return "## 💾 Backups\n*No backup files yet.*"

        total_size = sum(os.path.getsize(f) for f in backup_files)
        total_size_kb = round(total_size / 1024, 1)

        latest = backup_files[0]
        latest_name = os.path.basename(latest)
        latest_time = (
            datetime.fromtimestamp(os.path.getmtime(latest))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        latest_size_kb = round(os.path.getsize(latest) / 1024, 1)

        # Upload latest backup file to Slack and get permalink for canvas embed
        file_embed = ""
        if app and OPS_CHANNEL_ID:
            try:
                permalink = _upload_backup_file(app, latest, latest_name)
                if permalink:
                    file_embed = f"\n\n📎 [Download latest backup]({permalink})"
            except Exception as e:
                logger.warning(f"CANVAS: Could not upload backup file: {e}")

        return f"""## 💾 Backups
- **Files:** {count} backups ({total_size_kb} KB total)
- **Latest:** `{latest_name}` ({latest_size_kb} KB)
- **Last backup:** `{latest_time}`{file_embed}"""

    except Exception as e:
        logger.error(f"CANVAS: Failed to build backups section: {e}")
        return "## 💾 Backups\n*Error loading backup data.*"


def _ensure_backup_thread(app):
    """Get or create a pinned thread for backup file uploads.

    Returns the parent message ts, or None on failure.
    """
    settings = _load_settings()
    thread_ts = settings.get("backup_thread_ts")

    # Verify existing thread still exists
    if thread_ts:
        try:
            app.client.conversations_replies(channel=OPS_CHANNEL_ID, ts=thread_ts, limit=1)
            return thread_ts
        except SlackApiError:
            # Thread message was deleted — recreate
            thread_ts = None

    # Post a parent message and pin it
    try:
        result = send_message(
            app,
            OPS_CHANNEL_ID,
            "📎 *Birthday Backup Files*\nBackup uploads are posted as replies to this thread.",
        )
        thread_ts = result.get("ts")
        if thread_ts:
            _save_settings({"backup_thread_ts": thread_ts})
            try:
                app.client.pins_add(channel=OPS_CHANNEL_ID, timestamp=thread_ts)
            except SlackApiError as pin_err:
                error_code = pin_err.response.get("error", "")
                if error_code != "already_pinned":
                    logger.warning(f"CANVAS: Could not pin backup thread: {pin_err}")
            logger.info(f"CANVAS: Created backup thread: {thread_ts}")
            return thread_ts
    except SlackApiError as e:
        logger.error(f"CANVAS: Failed to create backup thread: {e}")

    return None


def _upload_backup_file(app, file_path, filename):
    """Upload backup file to a dedicated thread and return its permalink.

    Uses a pinned thread in the ops channel to keep backups organized.
    Old files are kept in the thread as historical audit trail — never deleted.
    """
    # Skip if same file already uploaded (persisted across restarts)
    file_mtime = os.path.getmtime(file_path)
    cache_key = f"{filename}:{file_mtime}"
    settings = _load_settings()
    if settings.get("backup_cache_key") == cache_key and settings.get("backup_permalink"):
        return settings["backup_permalink"]

    thread_ts = _ensure_backup_thread(app)
    if not thread_ts:
        return None

    # Upload file as a thread reply (old files stay for history)
    with open(file_path, "rb") as f:
        response = app.client.files_upload_v2(
            channel=OPS_CHANNEL_ID,
            thread_ts=thread_ts,
            file=f,
            filename=filename,
            title=f"Birthday Backup - {filename}",
        )

    if response.get("ok"):
        # Extract file ID
        files = response.get("files", [])
        file_id = files[0].get("id") if files else response.get("file", {}).get("id")
        if not file_id:
            return None

        # Get permalink from files.info
        permalink = None
        try:
            info_response = app.client.files_info(file=file_id)
            file_info = info_response.get("file", {})
            permalink = file_info.get("permalink")
        except SlackApiError as e:
            logger.warning(f"CANVAS: Could not get file info: {e}")

        if permalink:
            _save_settings(
                {
                    "backup_cache_key": cache_key,
                    "backup_permalink": permalink,
                }
            )
            logger.info(f"CANVAS: Uploaded backup file {filename} to thread")
            return permalink

    return None


def _replace_canvas_content(app, canvas_id, markdown):
    """Replace entire canvas content, working around Slack API quirks.

    The ``canvases.edit`` ``replace`` operation without a ``section_id`` is
    unreliable — it sometimes concatenates instead of replacing.  This helper
    first looks up all header-delimited sections, deletes them, and then
    inserts the new content at the start so the canvas is fully refreshed.
    """
    # Step 1: find existing sections to delete
    sections = []
    try:
        lookup = app.client.api_call(
            "canvases.sections.lookup",
            json={
                "canvas_id": canvas_id,
                "criteria": {"section_types": ["any_header"]},
            },
        )
        sections = lookup.get("sections", [])
    except SlackApiError as e:
        logger.debug(f"CANVAS: Section lookup failed, using plain replace: {e}")

    # Step 2: build changes — delete every old section, then insert fresh
    if sections:
        # Deduplicate section IDs (just in case)
        seen = set()
        unique = []
        for s in sections:
            sid = s.get("id")
            if sid and sid not in seen:
                seen.add(sid)
                unique.append(sid)

        changes = [{"operation": "delete", "section_id": sid} for sid in unique]
        changes.append(
            {
                "operation": "insert_at_start",
                "document_content": {"type": "markdown", "markdown": markdown},
            }
        )
        app.client.canvases_edit(canvas_id=canvas_id, changes=changes)
    else:
        # Fallback: replace without section_id (original behaviour)
        app.client.canvases_edit(
            canvas_id=canvas_id,
            changes=[
                {
                    "operation": "replace",
                    "document_content": {"type": "markdown", "markdown": markdown},
                }
            ],
        )


def _build_warnings_section():
    """Build recent warnings section, or return None if no active warnings."""
    now = datetime.now().astimezone()
    ttl_seconds = _warnings_ttl_seconds
    active = [
        (ts, text)
        for ts, text in list(_recent_warnings)
        if (now - ts).total_seconds() < ttl_seconds
    ]
    if not active:
        return None
    lines = "\n".join(f"- {text}" for _, text in reversed(active))
    return f"""## ⚠️ Recent Warnings
{lines}"""


# --- Public API ---


def record_change(change_text):
    """Record a recent change for display in the dashboard."""
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    _recent_changes.append(f"`{timestamp}` — {change_text}")


def record_warning(warning_text):
    """Record a warning for display in the canvas dashboard."""
    now = datetime.now().astimezone()
    display = f"`{now.strftime('%Y-%m-%d %H:%M %Z')}` — {warning_text}"
    _recent_warnings.append((now, display))


def clear_warnings():
    """Clear all warnings from the canvas dashboard."""
    _recent_warnings.clear()


def safe_record_warning(warning_text):
    """Fire-and-forget wrapper for record_warning. Never raises."""
    try:
        record_warning(warning_text)
    except Exception:
        pass


def update_canvas(app, reason="periodic", force=False):
    """
    Rebuild and replace the canvas dashboard content.

    Returns True on success, False on failure.
    Debounces rapid updates (30-second minimum interval) unless force=True.
    """
    global _last_update_time

    if not CANVAS_DASHBOARD_ENABLED or not OPS_CHANNEL_ID:
        return False

    # Debounce (skip for forced/admin updates)
    with _update_lock:
        now = datetime.now()
        if (
            not force
            and _last_update_time
            and (now - _last_update_time).total_seconds() < CANVAS_MIN_UPDATE_INTERVAL_SECONDS
        ):
            logger.debug(f"CANVAS: Skipping update (debounce), reason: {reason}")
            return False
        _last_update_time = now

    try:
        canvas_id = _ensure_canvas(app, OPS_CHANNEL_ID)
        if not canvas_id:
            return False

        markdown = _build_dashboard_markdown(app)
        _replace_canvas_content(app, canvas_id, markdown)

        # Reuse sd_total computed during _build_observances_section to avoid re-loading
        _update_channel_topic(app, OPS_CHANNEL_ID, sd_total=_last_sd_total)
        _save_settings({"canvas_updated_at": datetime.now().isoformat()})
        logger.info(f"CANVAS: Dashboard updated successfully (reason: {reason})")
        return True

    except SlackApiError as e:
        error_code = e.response.get("error", "")
        if error_code in ("canvas_not_found", "invalid_canvas"):
            # Canvas was deleted — clear stored ID and recreate immediately
            _save_canvas_id(None)
            logger.warning("CANVAS: Canvas was deleted, recreating...")
            try:
                canvas_id = _ensure_canvas(app, OPS_CHANNEL_ID)
                if canvas_id:
                    _replace_canvas_content(app, canvas_id, markdown)
                    _save_settings({"canvas_updated_at": datetime.now().isoformat()})
                    logger.info(f"CANVAS: Recreated and updated (reason: {reason})")
                    return True
            except Exception as retry_err:
                logger.error(f"CANVAS: Failed to recreate canvas: {retry_err}")
        else:
            logger.error(f"CANVAS: Failed to update dashboard: {e}")
        return False
    except Exception as e:
        logger.error(f"CANVAS: Unexpected error updating dashboard: {e}")
        return False


def update_canvas_async(app, reason="periodic"):
    """Update canvas in a background thread so callers never block."""
    thread = threading.Thread(
        target=update_canvas,
        args=(app, reason),
        daemon=True,
    )
    thread.start()


def get_canvas_status():
    """Get canvas dashboard status for admin commands."""
    settings = _load_settings()
    last_update = (
        _last_update_time.isoformat() if _last_update_time else settings.get("canvas_updated_at")
    )
    # Count only non-expired warnings
    now = datetime.now().astimezone()
    ttl_seconds = _warnings_ttl_seconds
    active_warnings = sum(
        1 for ts, _ in list(_recent_warnings) if (now - ts).total_seconds() < ttl_seconds
    )

    return {
        "enabled": CANVAS_DASHBOARD_ENABLED,
        "canvas_id": settings.get("canvas_id"),
        "channel_id": OPS_CHANNEL_ID,
        "recent_changes": len(_recent_changes),
        "active_warnings": active_warnings,
        "last_update": last_update,
        "backup_permalink": settings.get("backup_permalink"),
        "backup_cache_key": settings.get("backup_cache_key"),
        "backup_thread_ts": settings.get("backup_thread_ts"),
    }


def reset_canvas(app=None):
    """Delete the existing canvas and clear stored ID.

    Preserves backup thread settings so the existing pinned thread is reused.
    The caller is responsible for triggering a refresh to recreate the canvas.
    """
    try:
        settings = _load_settings()
        canvas_id = settings.get("canvas_id")

        # Delete the canvas from Slack
        if canvas_id and app:
            try:
                app.client.canvases_delete(canvas_id=canvas_id)
                logger.info(f"CANVAS: Deleted canvas {canvas_id}")
            except SlackApiError as e:
                error_code = e.response.get("error", "")
                if error_code not in ("canvas_not_found", "invalid_canvas"):
                    logger.warning(f"CANVAS: Could not delete canvas: {e}")

        # Only remove canvas-specific keys, keep backup thread intact
        for key in ("canvas_id", "canvas_updated_at"):
            settings.pop(key, None)
        with open(CANVAS_SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        logger.info("CANVAS: Canvas reset (backup thread preserved)")
        return True
    except Exception as e:
        logger.error(f"CANVAS: Failed to reset: {e}")
        return False


def clean_channel(app, channel_id=None):
    """
    Delete bot's own messages from the ops channel.

    Only deletes messages authored by the bot.
    Preserves the pinned backup thread and its replies.
    Returns count of deleted messages.
    """
    channel_id = channel_id or OPS_CHANNEL_ID
    if not channel_id:
        return 0

    # Protect the backup thread from being cleaned
    settings = _load_settings()
    backup_thread_ts = settings.get("backup_thread_ts")

    try:
        # Get bot's own user ID
        auth = app.client.auth_test()
        bot_user_id = auth.get("user_id")

        deleted = 0
        cursor = None

        while True:
            kwargs = {"channel": channel_id, "limit": SLACK_HISTORY_PAGE_SIZE}
            if cursor:
                kwargs["cursor"] = cursor

            result = app.client.conversations_history(**kwargs)
            messages = result.get("messages", [])

            for msg in messages:
                # Skip the pinned backup thread
                if backup_thread_ts and msg.get("ts") == backup_thread_ts:
                    continue

                # Only delete bot's own messages
                if msg.get("user") == bot_user_id or msg.get("bot_id"):
                    try:
                        app.client.chat_delete(channel=channel_id, ts=msg["ts"])
                        deleted += 1
                    except SlackApiError:
                        pass  # Skip messages we can't delete

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        logger.info(f"CANVAS: Cleaned {deleted} bot messages from channel {channel_id}")
        return deleted

    except SlackApiError as e:
        logger.error(f"CANVAS: Failed to clean channel: {e}")
        return 0
