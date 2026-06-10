"""
Special days command handling for BrightDayBot.

Handles user special days commands (view, search, stats) and admin commands
(add, remove, config, test). Features quoted string parsing for multi-word
parameters and comprehensive special days management.

Main functions:
- handle_special_command(): User-facing special days commands
- handle_admin_special_command_with_quotes(): Admin commands with quoted parsing
- handle_admin_special_command(): Admin commands (non-add operations)
- parse_quoted_args(): Parse command text with quoted arguments
"""

from datetime import datetime, timedelta

from config import (
    DATE_FORMAT,
    DEFAULT_ANNOUNCEMENT_TIME,
    ICS_MAX_CONSECUTIVE_FAILURES,
    UPCOMING_DAYS_DEFAULT,
    UPCOMING_DAYS_EXTENDED,
    get_logger,
)
from slack.client import get_username
from utils.sanitization import sanitize_slack_text

logger = get_logger("commands")


def _sort_upcoming_by_date(upcoming, days, reference_date=None):
    """Build a chronologically ordered dict from upcoming special days."""
    today = reference_date or datetime.now()
    sorted_upcoming = {}
    for i in range(days):
        date_str = (today + timedelta(days=i)).strftime("%d/%m")
        if date_str in upcoming:
            sorted_upcoming[date_str] = upcoming[date_str]
    return sorted_upcoming


def handle_special_command(args, user_id, say, app):
    """Handle user special days commands using Block Kit"""
    from slack.blocks import (
        build_special_day_stats_blocks,
        build_special_days_list_blocks,
    )
    from storage.special_days import (
        get_special_day_statistics,
        get_todays_special_days,
        get_upcoming_special_days,
        load_all_special_days,
    )

    # Default to showing today if no args
    if not args:
        args = ["today"]

    subcommand = args[0].lower()

    if subcommand == "today":
        # Show today's special days using Block Kit
        now = datetime.now()
        special_days = get_todays_special_days(reference_date=now)
        from utils.date_utils import format_date_european_short

        today_str = format_date_european_short(now)
        blocks, fallback = build_special_days_list_blocks(
            special_days, view_mode="today", date_filter=today_str
        )
        say(blocks=blocks, text=fallback)

    elif subcommand in ["week", "upcoming"]:
        # Show upcoming special days for the week using Block Kit
        now = datetime.now()
        upcoming = get_upcoming_special_days(UPCOMING_DAYS_DEFAULT, reference_date=now)

        sorted_upcoming = _sort_upcoming_by_date(
            upcoming, UPCOMING_DAYS_DEFAULT, reference_date=now
        )
        blocks, fallback = build_special_days_list_blocks(sorted_upcoming, view_mode="week")
        say(blocks=blocks, text=fallback)

    elif subcommand == "month":
        # Show special days for the extended lookahead period using Block Kit
        now = datetime.now()
        upcoming = get_upcoming_special_days(UPCOMING_DAYS_EXTENDED, reference_date=now)

        sorted_upcoming = _sort_upcoming_by_date(
            upcoming, UPCOMING_DAYS_EXTENDED, reference_date=now
        )
        blocks, fallback = build_special_days_list_blocks(sorted_upcoming, view_mode="month")
        say(blocks=blocks, text=fallback)

    elif subcommand == "list":
        # List upcoming special days by category using Block Kit (user-friendly view)
        category_filter = args[1] if len(args) > 1 else None
        all_days = load_all_special_days()

        if category_filter:
            all_days = [d for d in all_days if d.category.lower() == category_filter.lower()]

        blocks, fallback = build_special_days_list_blocks(
            all_days, view_mode="list", category_filter=category_filter
        )
        say(blocks=blocks, text=fallback)

    elif subcommand == "stats":
        # Show statistics using Block Kit
        stats = get_special_day_statistics()
        blocks, fallback = build_special_day_stats_blocks(stats)
        say(blocks=blocks, text=fallback)

    elif subcommand == "export":
        source_filter = args[1].lower() if len(args) > 1 else None
        _handle_special_day_export(source_filter, user_id, say, app)

    elif subcommand == "help":
        # Show help using Block Kit
        from slack.blocks import build_slash_help_blocks

        blocks, fallback = build_slash_help_blocks("special-day")
        say(blocks=blocks, text=fallback)

    else:
        # Unknown subcommand - show help
        from slack.blocks import build_slash_help_blocks

        blocks, fallback = build_slash_help_blocks("special-day")
        say(blocks=blocks, text=fallback)

    logger.info(f"SPECIAL: {get_username(app, user_id)} used special command: {' '.join(args)}")


def parse_quoted_args(command_text):
    """Parse command text with quoted arguments, handling spaces inside quotes"""
    parts = []
    current = ""
    in_quotes = False
    i = 0

    while i < len(command_text):
        char = command_text[i]

        if char == '"':
            if in_quotes:
                # End quote - add current part
                parts.append(current)
                current = ""
                in_quotes = False
            else:
                # Start quote
                in_quotes = True
        elif char == " " and not in_quotes:
            # Space outside quotes - end current part
            if current:
                parts.append(current)
                current = ""
        else:
            # Regular character
            current += char

        i += 1

    # Add final part if any
    if current:
        parts.append(current)

    return parts


def handle_admin_special_command_with_quotes(command_text, user_id, say, app):
    """Handle admin special days commands with quoted string parsing"""
    from storage.special_days import (
        SpecialDay,
        save_special_day,
    )

    username = get_username(app, user_id)

    # Parse quoted arguments
    args = parse_quoted_args(command_text)

    if not args:
        args = ["help"]

    subcommand = args[0].lower()

    if subcommand == "add":
        # Add a new special day: admin special add DD/MM "Name" "Category" "Description" ["emoji"] ["source"] ["url"]
        if len(args) < 5:
            say(
                'Usage: `admin special add DD/MM "Name" "Category" "Description" ["emoji"] ["source"] ["url"]`\n'
                "Examples:\n"
                '• `admin special add 15/03 "World Sleep Day" "Global Health" "Promoting healthy sleep"`\n'
                '• `admin special add 15/03 "World Sleep Day" "Global Health" "Promoting healthy sleep" "💤"`\n'
                '• `admin special add 15/03 "World Sleep Day" "Global Health" "Promoting healthy sleep" "💤" "World Sleep Society" "https://worldsleepday.org"`'
            )
            return

        try:
            date_str = args[1]
            name = args[2]
            category = args[3]
            description = args[4]
            emoji = args[5] if len(args) > 5 else ""
            source = args[6] if len(args) > 6 else "Custom"
            url = args[7] if len(args) > 7 else ""

            # Validate date format (DD/MM) using datetime
            date_obj = datetime.strptime(date_str, DATE_FORMAT)
            day, month = date_obj.day, date_obj.month

            # Basic URL validation if provided
            if url and not (url.startswith("http://") or url.startswith("https://")):
                say("❌ URL must start with http:// or https://")
                return

            # Validate category
            from config import SPECIAL_DAYS_CATEGORIES

            if category not in SPECIAL_DAYS_CATEGORIES:
                say(f"Invalid category. Must be one of: {', '.join(SPECIAL_DAYS_CATEGORIES)}")
                return

            special_day = SpecialDay(
                date=f"{day:02d}/{month:02d}",
                name=name,
                category=category,
                description=description,
                emoji=emoji,
                enabled=True,
                source=source,
                url=url,
            )

            if save_special_day(special_day, app, username):
                source_info = f" (Source: {source})" if source != "Custom" else ""
                url_info = f" - {url}" if url else ""
                say(
                    f"✅ Added special day: {emoji} *{name}* on {date_str} ({category}){source_info}{url_info}"
                )
                logger.info(
                    f"ADMIN_SPECIAL: {username} added special day: {name} on {date_str} with source: {source}"
                )
            else:
                say("❌ Failed to add special day. Check logs for details.")

        except (ValueError, IndexError):
            say(
                '❌ Invalid format. Use: `admin special add DD/MM "Name" "Category" "Description" ["emoji"] ["source"] ["url"]`\n'
                'Example: `admin special add 15/03 "World Sleep Day" "Global Health" "Promoting healthy sleep" "💤" "World Sleep Society" "https://worldsleepday.org"`'
            )

    else:
        # For non-add commands, fall back to the original handler
        # Convert back to simple args for compatibility
        simple_args = command_text.split()
        handle_admin_special_command(simple_args, user_id, say, app)


def _show_observance_status(source_name, source_key, say):
    """Show cache status for an observance source (UN/UNESCO/WHO)."""
    try:
        from integrations.observances import get_enabled_sources

        status_fn = None
        for name, _refresh, status in get_enabled_sources():
            if name == source_name:
                status_fn = status
                break

        if not status_fn:
            say(f"❌ {source_name} observances not enabled")
            return

        status = status_fn()
        last_updated = status.get("last_updated")
        last_str = "Never"
        if last_updated:
            last_dt = datetime.fromisoformat(last_updated)
            last_str = f"`{last_dt.strftime('%Y-%m-%d %H:%M')}`"

        say(
            f"📊 *{source_name} Observances Cache Status*\n\n"
            f"• Cache exists: {'✅ Yes' if status['cache_exists'] else '❌ No'}\n"
            f"• Cache fresh: {'✅ Yes' if status['cache_fresh'] else '⚠️ Stale'}\n"
            f"• Last updated: {last_str}\n"
            f"• Observances cached: {status['observance_count']}\n\n"
            f"*Source:* {status['source_url']}\n\n"
            f"_Use `admin special {source_key}-refresh` to force update._"
        )
    except Exception as e:
        say(f"❌ Failed to get {source_name} cache status: {e}")
        logger.error(f"ADMIN_SPECIAL: Failed to get {source_name} status: {e}")


def _refresh_observance(source_name, source_key, say, username):
    """Force refresh an observance source cache (UN/UNESCO/WHO)."""
    try:
        from integrations.observances import get_enabled_sources

        refresh_fn = None
        for name, refresh, _status in get_enabled_sources():
            if name == source_name:
                refresh_fn = refresh
                break

        if not refresh_fn:
            say(f"❌ {source_name} observances not enabled")
            return

        say(f"🔄 Refreshing {source_name} observances cache...")
        stats = refresh_fn(force=True)

        if stats.get("error"):
            say(f"❌ Refresh failed: {stats['error']}")
        else:
            say(f"✅ {source_name} observances cache refreshed: {stats['fetched']} observances")
            logger.info(f"ADMIN_SPECIAL: {username} refreshed {source_name} cache")
    except Exception as e:
        say(f"❌ Refresh error: {e}")
        logger.error(f"ADMIN_SPECIAL: {source_name} refresh failed: {e}")


def handle_admin_special_command(args, user_id, say, app):
    """Handle admin special days commands (non-add commands only)"""
    from config import CALENDARIFIC_API_KEY, CALENDARIFIC_ENABLED
    from integrations.calendarific import get_calendarific_client
    from services.special_day import generate_special_day_message
    from storage.special_days import (
        get_special_days_for_date,
        load_all_special_days,
        load_special_days_config,
        remove_special_day,
        save_special_days_config,
        update_category_status,
    )
    from utils.date_utils import format_date_european_short

    username = get_username(app, user_id)

    if not args:
        args = ["help"]

    subcommand = args[0].lower()

    if subcommand == "remove":
        # Remove a special day: admin special remove DD/MM [name]
        if len(args) < 2:
            say("Usage: `admin special remove DD/MM [name]`")
            return

        date_str = args[1]
        name = args[2] if len(args) > 2 else None

        if remove_special_day(date_str, name, app, username):
            say(f"✅ Removed special day(s) for {date_str}")
            logger.info(f"ADMIN_SPECIAL: {username} removed special day for {date_str}")
        else:
            say(f"❌ No special day found for {date_str}")

    elif subcommand == "list":
        # List all special days with admin details using Block Kit
        from slack.blocks import build_special_days_list_blocks

        category_filter = args[1] if len(args) > 1 else None
        all_days = load_all_special_days()

        if category_filter:
            all_days = [d for d in all_days if d.category.lower() == category_filter.lower()]

        blocks, fallback = build_special_days_list_blocks(
            all_days, view_mode="list", category_filter=category_filter, admin_view=True
        )
        say(blocks=blocks, text=fallback)

    elif subcommand == "categories":
        # Manage category settings
        config = load_special_days_config()
        categories_enabled = config.get("categories_enabled", {})

        if len(args) == 1:
            # Show current status
            message = "📋 *Special Days Categories:*\n\n"
            from config import SPECIAL_DAYS_CATEGORIES

            for category in SPECIAL_DAYS_CATEGORIES:
                status = "✅" if categories_enabled.get(category, True) else "❌"
                message += f"{status} {category}\n"
            say(message)

        elif len(args) >= 3 and args[1] in ["enable", "disable"]:
            # Enable/disable a category
            action = args[1]
            category = " ".join(args[2:])
            enabled = action == "enable"

            if update_category_status(category, enabled):
                say(f"✅ {category} category {'enabled' if enabled else 'disabled'}")
                logger.info(f"ADMIN_SPECIAL: {username} {action}d category: {category}")
            else:
                say(f"❌ Invalid category: {category}")

    elif subcommand == "test":
        # Test announcement for a specific date
        if len(args) < 2:
            # Test today
            test_date = datetime.now()
        else:
            try:
                # Parse date (DD/MM) using datetime
                date_str = args[1]
                date_obj = datetime.strptime(date_str, DATE_FORMAT)
                test_date = datetime.now().replace(day=date_obj.day, month=date_obj.month)
            except (ValueError, IndexError):
                say("Invalid date format. Use DD/MM")
                return

        special_days = get_special_days_for_date(test_date)

        if special_days:
            test_date_str = format_date_european_short(test_date)
            say(f"🧪 Testing special day announcement for {test_date_str}...")

            from config import SPECIAL_DAY_CONSOLIDATED_ENABLED, SPECIAL_DAYS_PERSONALITY
            from services.special_day import generate_special_day_details
            from slack.messaging import send_message

            # Consolidated mode for multiple observances
            if SPECIAL_DAY_CONSOLIDATED_ENABLED and len(special_days) > 1:
                say(
                    f"📋 Generating consolidated test announcement ({len(special_days)} observances)..."
                )

                from services.special_day import generate_consolidated_intro_message
                from slack.blocks import build_consolidated_special_day_blocks

                intro = generate_consolidated_intro_message(special_days, app=app)

                from config import run_parallel

                def _generate_for_sd(sd):
                    teaser = generate_special_day_message(
                        [sd],
                        test_mode=True,
                        app=app,
                        use_teaser=True,
                        suppress_mention=True,
                        test_date=test_date,
                    )
                    details = generate_special_day_details([sd], app=app, test_date=test_date)
                    return teaser or "", details or ""

                results = run_parallel(_generate_for_sd, special_days)

                teasers = {}
                detailed_contents = {}
                for sd, result in results.items():
                    if result:
                        teasers[sd.name] = result[0]
                        detailed_contents[sd.name] = result[1]
                    else:
                        logger.error(f"SPECIAL_DAYS: Failed to generate for {sd.name}")
                        teasers[sd.name] = ""
                        detailed_contents[sd.name] = ""

                blocks, fallback_text = build_consolidated_special_day_blocks(
                    special_days,
                    intro,
                    teasers,
                    detailed_contents,
                    personality=SPECIAL_DAYS_PERSONALITY,
                    observance_date=test_date.strftime("%d/%m"),
                )

                send_message(app, user_id, fallback_text, blocks)
                say(
                    f"\n✅ Sent consolidated announcement ({len(special_days)} observances) to your DM"
                )

            # Individual mode: separate messages per observance
            elif len(special_days) >= 1:
                say(f"📋 Sending {len(special_days)} separate test announcement(s)")

                # Pre-generate all AI content
                from config import run_parallel

                def _generate_individual(sd):
                    teaser = generate_special_day_message(
                        [sd],
                        test_mode=True,
                        app=app,
                        use_teaser=True,
                        test_date=test_date,
                    )
                    details = generate_special_day_details([sd], app=app, test_date=test_date)
                    return teaser, details

                results = run_parallel(_generate_individual, special_days)

                generated = {}
                for sd, result in results.items():
                    if result:
                        generated[sd.name] = result
                    else:
                        logger.error(f"SPECIAL_DAYS: Failed to generate for {sd.name}")
                        generated[sd.name] = (None, None)

                # Send sequentially
                from slack.blocks import build_special_day_blocks

                for special_day in special_days:
                    message, detailed_content = generated.get(special_day.name, (None, None))
                    if message:
                        blocks, fallback_text = build_special_day_blocks(
                            [special_day],
                            message,
                            personality=SPECIAL_DAYS_PERSONALITY,
                            detailed_content=detailed_content,
                        )
                        send_message(app, user_id, fallback_text, blocks)
                    else:
                        say(f"❌ Failed to generate message for {special_day.name}")

                say(f"\n✅ Sent {len(special_days)} separate announcement(s) to your DM")
        else:
            test_date_str = format_date_european_short(test_date)
            say(f"No special days found for {test_date_str}")

    elif subcommand == "config":
        # Show or update configuration
        config = load_special_days_config()

        if len(args) == 1:
            # Show current config
            message = "⚙️ *Special Days Configuration:*\n\n"
            message += (
                f"• Feature: {'✅ Enabled' if config.get('enabled', False) else '❌ Disabled'}\n"
            )
            message += f"• Personality: {config.get('personality', 'chronicler')}\n"
            message += f"• Announcement time: {config.get('announcement_time', DEFAULT_ANNOUNCEMENT_TIME)}\n"
            message += f"• Channel: {config.get('channel_override') or 'Using birthday channel'}\n"
            message += (
                f"• Image generation: {'✅' if config.get('image_generation', False) else '❌'}\n"
            )
            say(message)

        elif len(args) >= 3:
            # Update config
            setting = args[1].lower()
            value = " ".join(args[2:])

            if setting == "personality":
                config["personality"] = value
            elif setting == "time":
                config["announcement_time"] = value
            elif setting == "channel":
                config["channel_override"] = value if value != "none" else None
            elif setting == "images":
                config["image_generation"] = value.lower() in ["true", "on", "yes", "1"]
            elif setting == "enable":
                config["enabled"] = True
            elif setting == "disable":
                config["enabled"] = False
            else:
                say(f"Unknown setting: {setting}")
                return

            if save_special_days_config(config):
                say(f"✅ Updated special days {setting}")
                logger.info(f"ADMIN_SPECIAL: {username} updated config: {setting} = {value}")
            else:
                say("❌ Failed to save configuration")

    elif subcommand == "mode":
        # Switch between daily and weekly announcement modes
        from config import WEEKDAY_NAMES
        from storage.special_days import (
            get_pending_mode_transition,
            get_special_days_mode,
            get_weekly_day,
            set_special_days_mode,
        )

        current_mode = get_special_days_mode()
        current_day = get_weekly_day()
        current_day_name = WEEKDAY_NAMES[current_day].capitalize()
        pending = get_pending_mode_transition()

        if len(args) == 1:
            # Show current mode
            transition_note = ""
            if pending:
                eff = pending["effective_date"].strftime("%A, %b %d")
                transition_note = f"\n• _Switching to *{pending['target_mode']}* on {eff}_"

            if current_mode == "weekly":
                message = f"""📅 *Special Days Announcement Mode*

• Current mode: *Weekly*
• Digest day: *{current_day_name}*{transition_note}

In weekly mode, a single digest of all upcoming observances is posted once per week."""
            else:
                message = f"""📅 *Special Days Announcement Mode*

• Current mode: *Daily*{transition_note}

In daily mode, individual announcements are posted each day with observances."""
            say(message)

        elif args[1].lower() == "daily":
            # Switch to daily mode
            if set_special_days_mode("daily"):
                say(
                    "✅ Switched to *daily* mode. Individual announcements will be posted each day.\n\n"
                    "Change takes effect immediately."
                )
                logger.info(f"ADMIN_SPECIAL: {username} switched to daily mode")
            else:
                say("❌ Failed to switch mode")

        elif args[1].lower() == "weekly":
            # Switch to weekly mode
            # Check if a specific day was provided
            if len(args) >= 3:
                day_input = args[2].lower()
                if day_input in WEEKDAY_NAMES:
                    weekly_day = WEEKDAY_NAMES.index(day_input)
                elif day_input.isdigit() and 0 <= int(day_input) <= 6:
                    weekly_day = int(day_input)
                else:
                    say(
                        f"❌ Invalid day: {day_input}\n"
                        f"Use a day name (monday, tuesday, etc.) or number (0-6 where 0=Monday)"
                    )
                    return
            else:
                weekly_day = current_day  # Keep existing day

            day_name = WEEKDAY_NAMES[weekly_day].capitalize()

            if set_special_days_mode("weekly", weekly_day):
                # Check if transition is deferred
                pending = get_pending_mode_transition()
                if pending:
                    eff = pending["effective_date"].strftime("%A, %b %d")
                    say(
                        f"✅ Switching to *weekly* mode. Digest will be posted every *{day_name}*.\n\n"
                        f"Daily announcements will continue until *{eff}*."
                    )
                else:
                    say(f"✅ Switched to *weekly* mode. Digest will be posted every *{day_name}*.")
                logger.info(f"ADMIN_SPECIAL: {username} switched to weekly mode on {day_name}")
            else:
                say("❌ Failed to switch mode")

        else:
            say(
                "Usage:\n"
                "• `admin special mode` - Show current mode\n"
                "• `admin special mode daily` - Switch to daily mode\n"
                "• `admin special mode weekly` - Switch to weekly (default Monday)\n"
                "• `admin special mode weekly friday` - Switch to weekly on Friday"
            )

    elif subcommand == "verify":
        # Verify special days data
        from storage.special_days import verify_special_days

        results = verify_special_days()

        message = "🔍 *Special Days Verification Report:*\n\n"
        message += "*Statistics:*\n"
        message += f"• Total days: {results['stats']['total']}\n"
        message += f"• Days with source: {results['stats']['with_source']}\n"
        message += f"• Days with URL: {results['stats']['with_url']}\n\n"

        message += "*By Category:*\n"
        for cat, count in results["stats"]["by_category"].items():
            message += f"• {cat}: {count}\n"

        # Report issues
        issues_found = False
        if results["missing_sources"]:
            issues_found = True
            message += f"\n⚠️ *Missing Sources:* {len(results['missing_sources'])} days\n"
            if len(results["missing_sources"]) <= 5:
                for day in results["missing_sources"]:
                    message += f"  - {day}\n"
            else:
                message += "  (showing first 5)\n"
                for day in results["missing_sources"][:5]:
                    message += f"  - {day}\n"

        if results["duplicate_dates"]:
            issues_found = True
            message += "\n⚠️ *Duplicate Dates:*\n"
            for date, names in results["duplicate_dates"].items():
                message += f"  • {date}: {', '.join(names)}\n"

        if results["invalid_dates"]:
            issues_found = True
            message += f"\n❌ *Invalid Dates:* {len(results['invalid_dates'])}\n"

        if not issues_found:
            message += "\n✅ All data validation checks passed!"

        say(message)
        logger.info(f"ADMIN_SPECIAL: {username} ran verification")

    elif subcommand == "import":
        # Import special days from CSV
        say(
            "📥 Import feature not yet implemented. Please add special days individually or edit the CSV file directly."
        )

    elif subcommand == "calendarific-refresh":
        # Calendarific: Force weekly prefetch
        if not CALENDARIFIC_ENABLED:
            say("❌ Calendarific API is not enabled. Set `CALENDARIFIC_ENABLED=true` in .env")
            return

        if not CALENDARIFIC_API_KEY:
            say("❌ Calendarific API key not configured. Add `CALENDARIFIC_API_KEY=...` to .env")
            return

        try:
            client = get_calendarific_client()

            # Parse args: calendarific-refresh [source_id|force]
            force = False
            source_id = None
            if len(args) > 1:
                arg = args[1].lower()
                if arg == "force":
                    force = True
                else:
                    source_id = arg
            if len(args) > 2 and args[2].lower() == "force":
                force = True

            if source_id:
                # Refresh specific source
                src = next((s for s in client.sources if s.id == source_id), None)
                if not src:
                    available = ", ".join(s.id for s in client.sources)
                    say(f"❌ Source `{source_id}` not found. Available: {available}")
                    return
                say(f"🔄 Refreshing *{src.label}* ({source_id})...")
                stats = client._prefetch_yearly(src, force=True)
                results = {source_id: stats}
            else:
                say("🔄 Refreshing Calendarific cache (all sources)...")
                results = client.prefetch_all(force=force)

            # Aggregate stats across sources
            lines = []
            for src_id, stats in results.items():
                src = next((s for s in client.sources if s.id == src_id), None)
                label = src.label if src else src_id
                if "error" in stats:
                    lines.append(f"• ❌ *{label}*: {stats['error']}")
                elif "skipped" in stats:
                    lines.append(f"• ⏭️ *{label}*: {stats['skipped']}")
                else:
                    lines.append(
                        f"• ✅ *{label}*: {stats.get('fetched', 0)} holidays, "
                        f"{stats.get('api_calls', 0)} API calls"
                    )

            say("✅ *Calendarific Prefetch Complete*\n\n" + "\n".join(lines))
            logger.info(f"ADMIN_SPECIAL: {username} ran Calendarific refresh")

        except Exception as e:
            say(f"❌ Prefetch error: {e}")
            logger.error(f"ADMIN_SPECIAL: Calendarific refresh failed: {e}")

    elif subcommand in ["observances-status", "observances", "sources"]:
        # Combined status for all observance sources
        try:
            from integrations.observances import get_enabled_sources

            sources = get_enabled_sources()
            if not sources:
                say("ℹ️ No observance sources are enabled.")
                return

            lines = []
            for name, _refresh_fn, status_fn in sources:
                status = status_fn()
                fresh = "✅" if status["cache_fresh"] else "⚠️"
                count = status["observance_count"]
                lines.append(f"• {name}: {fresh} {count} days")

            status_lines = "\n".join(lines)
            message = f"""📊 *Observance Sources Status*

{status_lines}

_Use `admin special [un|unesco|who]-status` for details._
_Use `admin special [un|unesco|who]-refresh` or `all-refresh` to force update._"""
            say(message)

        except Exception as e:
            say(f"❌ Failed to get observances status: {e}")
            logger.error(f"ADMIN_SPECIAL: Failed to get observances status: {e}")

    elif subcommand in ["un-status", "un"]:
        _show_observance_status("UN", "un", say)
    elif subcommand == "un-refresh":
        _refresh_observance("UN", "un", say, username)
    elif subcommand in ["unesco-status", "unesco"]:
        _show_observance_status("UNESCO", "unesco", say)
    elif subcommand == "unesco-refresh":
        _refresh_observance("UNESCO", "unesco", say, username)
    elif subcommand in ["who-status", "who"]:
        _show_observance_status("WHO", "who", say)
    elif subcommand == "who-refresh":
        _refresh_observance("WHO", "who", say, username)

    elif subcommand == "all-refresh":
        # Refresh all observance sources at once
        from integrations.observances import get_enabled_sources

        sources = get_enabled_sources()
        if not sources:
            say("ℹ️ No observance sources are enabled.")
            return

        say("🔄 Refreshing all observance sources...")

        results = []
        for name, refresh_fn, _status_fn in sources:
            try:
                stats = refresh_fn(force=True)
                if stats.get("error"):
                    results.append(f"• {name}: ❌ {stats['error']}")
                else:
                    results.append(f"• {name}: ✅ {stats['fetched']} observances")
            except Exception as e:
                results.append(f"• {name}: ❌ {e}")

        say("📊 *Refresh Results*\n\n" + "\n".join(results))
        logger.info(f"ADMIN_SPECIAL: {username} refreshed all observance caches")

    elif subcommand == "calendarific-status":
        if not CALENDARIFIC_ENABLED:
            say("📊 *Calendarific:* ❌ Disabled — set `CALENDARIFIC_ENABLED=true` in .env")
            return

        try:
            client = get_calendarific_client()
            status = client.get_api_status()

            last_prefetch = status.get("last_prefetch")
            last_str = "Never"
            if last_prefetch:
                last_dt = datetime.fromisoformat(last_prefetch)
                last_str = f"`{last_dt.strftime('%Y-%m-%d %H:%M')}`"

            # Per-source breakdown
            source_lines = []
            for sid, info in status.get("sources", {}).items():
                flag = "✅" if info["enabled"] else "❌"
                count = info.get("holiday_count", 0)
                fresh = "🟢" if info.get("cache_fresh") else "🟡"
                updated = info.get("last_updated", "—")
                if isinstance(updated, str) and "T" in updated:
                    updated = f"`{updated[:10]}`"
                source_lines.append(
                    f"• {flag} *{info['label']}* ({info['country']}) — {fresh} {count} holidays, updated {updated}"
                )
            sources_text = "\n".join(source_lines) if source_lines else "• No sources configured"

            say(
                f"📊 *Calendarific API Status*\n\n"
                f"• API Key: {'✅ Configured' if status['api_key_configured'] else '❌ Missing'}\n"
                f"• API calls: {status['month_calls']} / {status['monthly_limit']}\n"
                f"• Total holidays: {status['holiday_count']}\n"
                f"• Last prefetch: {last_str}\n"
                f"• Needs refresh: {'⚠️ Yes' if status['needs_prefetch'] else '✅ No'}\n\n"
                f"*Sources:*\n{sources_text}\n\n"
                f"_Use `admin special calendarific-toggle <source_id>` to enable/disable a source_"
            )

        except Exception as e:
            say(f"❌ Failed to get API status: {e}")
            logger.error(f"ADMIN_SPECIAL: Failed to get Calendarific status: {e}")

    elif subcommand == "calendarific-emojis":
        if not CALENDARIFIC_ENABLED:
            say("❌ Calendarific not enabled")
            return
        try:
            client = get_calendarific_client()
            source_id = args[1].lower() if len(args) > 1 else None
            targets = (
                [s for s in client.get_enabled_sources() if s.id == source_id]
                if source_id
                else client.get_enabled_sources()
            )
            if not targets:
                available = ", ".join(s.id for s in client.sources)
                say(f"❌ Source not found. Available: {available}")
                return

            say(f"🎨 Assigning emojis via AI for {len(targets)} source(s)...")
            total = 0
            for src in targets:
                cache_data = client._load_cache(src)
                all_holidays = []
                for entry in cache_data.get("entries", {}).values():
                    all_holidays.extend(entry.get("holidays", []))
                if all_holidays:
                    client._enrich_holidays_with_emojis(all_holidays)
                    client._save_cache(src, cache_data)
                    total += len(all_holidays)
                    say(f"• ✅ *{src.label}*: {len(all_holidays)} holidays enriched")
                else:
                    say(f"• ⏭️ *{src.label}*: no cached holidays")

            logger.info(f"ADMIN_SPECIAL: {username} enriched {total} holidays with AI emojis")
        except Exception as e:
            say(f"❌ Emoji enrichment failed: {e}")

    elif subcommand == "calendarific-toggle" and len(args) > 1:
        source_id = args[1].lower()
        try:
            client = get_calendarific_client()
            toggled = False
            for src in client.sources:
                if src.id == source_id:
                    src.enabled = not src.enabled
                    client.save_source_state()
                    new_state = "✅ enabled" if src.enabled else "❌ disabled"
                    say(f"Calendarific source *{src.label}* ({source_id}) is now {new_state}")
                    logger.info(
                        f"ADMIN_SPECIAL: {username} toggled Calendarific source {source_id}"
                    )
                    toggled = True
                    break
            if not toggled:
                available = ", ".join(s.id for s in client.sources)
                say(f"❌ Source `{source_id}` not found. Available: {available}")
        except Exception as e:
            say(f"❌ Toggle failed: {e}")

    elif subcommand == "ics-list":
        from integrations.ics_feed import get_ics_feed_client

        client = get_ics_feed_client()
        if not client.subscriptions:
            say("📅 No ICS subscriptions configured. Use `admin special ics-add` to add one.")
        else:
            lines = []
            for s in client.subscriptions:
                flag = "✅" if s.enabled else "❌"
                parts = [f"• {flag} *{s.label}* (`{s.id}`) — {s.event_count} events"]
                if s.consecutive_failures > 0:
                    parts.append(
                        f" ({s.consecutive_failures}/{ICS_MAX_CONSECUTIVE_FAILURES} failures)"
                    )
                if s.last_error:
                    parts.append(f" ⚠️ {s.last_error}")
                lines.append("".join(parts))
            say("📅 *ICS Subscriptions*\n\n" + "\n".join(lines))

    elif subcommand == "ics-add":
        from integrations.ics_feed import get_ics_feed_client

        if len(args) < 3:
            say("❌ Usage: `admin special ics-add <url> <label> [category] [emoji]`")
        else:
            url = args[1]
            label = sanitize_slack_text(args[2].strip('"').strip("'"), max_length=100)
            category = sanitize_slack_text(args[3], max_length=50) if len(args) > 3 else "Company"
            emoji = args[4][:10] if len(args) > 4 else "📅"

            say(f"🔄 Subscribing to `{label}`...")
            client = get_ics_feed_client()
            success, message = client.add_subscription(url, label, category, emoji, user_id)
            say(f"{'✅' if success else '❌'} {message}")
            if success:
                logger.info(f"ADMIN_SPECIAL: {username} added ICS subscription: {label}")
            else:
                logger.warning(f"ADMIN_SPECIAL: {username} failed to add ICS subscription: {label}")

    elif subcommand == "ics-remove" and len(args) > 1:
        from integrations.ics_feed import get_ics_feed_client

        success, message = get_ics_feed_client().remove_subscription(args[1])
        say(f"{'✅' if success else '❌'} {message}")
        logger.info(f"ADMIN_SPECIAL: {username} removed ICS subscription: {args[1]}")

    elif subcommand == "ics-toggle" and len(args) > 1:
        from integrations.ics_feed import get_ics_feed_client

        success, message = get_ics_feed_client().toggle_subscription(args[1])
        say(f"{'✅' if success else '❌'} {message}")
        logger.info(f"ADMIN_SPECIAL: {username} toggled ICS subscription: {args[1]}")

    elif subcommand == "ics-refresh":
        from integrations.ics_feed import get_ics_feed_client

        client = get_ics_feed_client()
        sub_id = args[1] if len(args) > 1 else None

        if sub_id:
            say(f"🔄 Refreshing `{sub_id}`...")
            stats = client.refresh_subscription(sub_id, force=True)
            if stats.get("error"):
                say(f"❌ {stats['error']}")
            else:
                say(f"✅ {stats.get('event_count', 0)} events loaded")
        else:
            say("🔄 Refreshing all ICS subscriptions...")
            results = client.refresh_all(force=True)
            if not results:
                say("📅 No enabled ICS subscriptions to refresh.")
            else:
                lines = []
                for sid, stats in results.items():
                    sub = next((s for s in client.subscriptions if s.id == sid), None)
                    label = sub.label if sub else sid
                    if stats.get("error"):
                        lines.append(f"• ❌ *{label}*: {stats['error']}")
                    else:
                        lines.append(f"• ✅ *{label}*: {stats.get('event_count', 0)} events")
                say("✅ *ICS Refresh Complete*\n\n" + "\n".join(lines))
        logger.info(f"ADMIN_SPECIAL: {username} refreshed ICS subscriptions")

    elif subcommand == "ics-test" and len(args) > 1:
        from integrations.ics_feed import get_ics_feed_client

        say("🔄 Testing feed...")
        result = get_ics_feed_client().preview_feed(args[1])
        if result.get("error"):
            say(f"❌ {result['error']}")
        else:
            count = result["event_count"]
            sample = result.get("sample", [])
            lines = [f"✅ Found *{count}* events"]
            for ev in sample:
                name = sanitize_slack_text(ev["name"], max_length=200)
                lines.append(f"  • {ev.get('emoji', '📅')} {name} ({ev['date']})")
            if count > 5:
                lines.append(f"  _...and {count - 5} more_")
            say("\n".join(lines))
        logger.info(f"ADMIN_SPECIAL: {username} tested ICS feed: {args[1]}")

    else:
        from slack.blocks.help import get_special_days_help_text

        say(f"*🌟 Special Days Commands:*\n\n{get_special_days_help_text()}")

    logger.info(
        f"ADMIN_SPECIAL: {username} ({user_id}) used admin special command: {' '.join(args)}"
    )


def _handle_special_day_export(source_filter, user_id, say, app):
    """
    Export special days as an ICS calendar file.

    Args:
        source_filter: Optional source to filter by (un/unesco/who/calendarific/custom), or None for all
        user_id: Slack user ID
        say: Slack say/respond function
        app: Slack app instance
    """
    import os
    import tempfile

    from slack.messaging import send_message_with_file
    from storage.special_days import load_all_special_days

    valid_sources = {"un", "unesco", "who", "calendarific", "custom"}

    # Treat "all" same as no filter
    if source_filter == "all":
        source_filter = None

    if source_filter and source_filter not in valid_sources:
        say(
            text=f"Unknown source `{source_filter}`. "
            f"Valid sources: {', '.join(f'`{s}`' for s in sorted(valid_sources))}\n"
            f"Or omit for all sources (deduplicated)."
        )
        return

    # Load all days (already deduplicated)
    all_days = load_all_special_days()

    # Filter by source if specified
    if source_filter:
        all_days = [d for d in all_days if d.source.lower() == source_filter]

    if not all_days:
        label = f" from `{source_filter}`" if source_filter else ""
        say(text=f"No special days{label} to export.")
        return

    # Generate ICS
    source_label = source_filter.upper() if source_filter else None
    from utils.ics import generate_special_days_ics

    ics_content = generate_special_days_ics(all_days, source_label)

    logger.info(
        f"EXPORT: Generated special days calendar with {len(all_days)} events for {user_id}"
    )

    # Create temp file and upload
    temp_file_path = None
    try:
        prefix = f"special_days_{source_filter}_" if source_filter else "special_days_"
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ics",
            prefix=prefix,
            delete=False,
            encoding="utf-8",
        ) as temp_file:
            temp_file_path = temp_file.name
            temp_file.write(ics_content)

        source_text = f" ({source_label})" if source_label else ""
        message = (
            f"📅 *Special Days Calendar Export{source_text}* — {len(all_days)} observances\n\n"
            f"Import this `.ics` file into your calendar app "
            f"(Google Calendar, Outlook, Apple Calendar).\n"
            f"💡 _Tip: Import into a *new calendar* so you can remove all events at once later._"
        )

        success = send_message_with_file(app, user_id, message, temp_file_path)

        if success:
            say(
                text=f"✅ Calendar exported! Check your DMs for the `.ics` file "
                f"with {len(all_days)} special days{source_text}."
            )
        else:
            say(
                text=f"*Special Days Calendar Export{source_text}* — {len(all_days)} observances\n\n"
                f"Copy the content below and save as `special_days.ics`:\n\n"
                f"```\n{ics_content}\n```"
            )

    except Exception as e:
        logger.error(f"EXPORT_ERROR: Failed to create/upload special days calendar: {e}")
        say(
            text=f"*Special Days Calendar Export* — {len(all_days)} observances\n\n"
            f"Copy the content below and save as `special_days.ics`:\n\n"
            f"```\n{ics_content}\n```"
        )

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.unlink(temp_file_path)
            except Exception:
                pass
