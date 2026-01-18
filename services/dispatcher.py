"""
User command processing for BrightDayBot.

Central command router that dispatches to specialized handler modules.
Handles user commands (birthday management) and admin commands via delegation.
Features multi-step confirmation system and permission-based access control.

Main function: handle_command(). Routes to:
- birthday_commands: list, check, remind
- admin_commands: stats, config, model, cache, status, timezone, etc.
- test_commands: test, test-*, admin test-*
- special_commands: special days management
"""

from datetime import datetime, timezone

from config import (
    TIMEOUTS,
    get_logger,
)
from personality_config import get_personality_config
from services.birthday import send_reminder_to_users
from services.message import (
    get_random_personality_name,
)
from slack.client import (
    get_user_mention,
    get_username,
    is_admin,
)
from storage.birthdays import (
    CELEBRATION_STYLE_EMOJIS,
    CELEBRATION_STYLES,
    get_user_preferences,
    remove_birthday,
    save_birthday,
)
from storage.settings import get_current_personality_name
from utils.date import (
    calculate_age,
    check_if_birthday_today,
    date_to_words,
    extract_date,
    get_star_sign,
)

# Confirmation timeout from centralized config
CONFIRMATION_TIMEOUT_MINUTES = TIMEOUTS.get("confirmation_minutes", 5)

# Import from split handler modules
from commands.admin_commands import (
    handle_admin_add_command,
    handle_admin_list_command,
    handle_admin_remove_command,
    handle_announce_command,
    handle_backup_command,
    handle_cache_command,
    handle_config_command,
    handle_model_command,
    handle_personality_command,
    handle_restore_command,
    handle_stats_command,
    handle_status_command,
    handle_timezone_command,
)
from commands.birthday_commands import (
    handle_check_command,
    handle_list_command,
    handle_remind_command,
    send_immediate_birthday_announcement,
)
from commands.special_commands import (
    handle_admin_special_command,
    handle_admin_special_command_with_quotes,
    handle_special_command,
)
from commands.test_commands import (
    handle_test_birthday_command,
    handle_test_block_command,
    handle_test_blockkit_command,
    handle_test_bot_celebration_command,
    handle_test_command,
    handle_test_external_backup_command,
    handle_test_file_upload_command,
    handle_test_join_command,
    handle_test_upload_command,
    handle_test_upload_multi_command,
    parse_test_command_args,
)

logger = get_logger("commands")

# Confirmation state management for mass notification commands
# Stores pending confirmations: {user_id: {"action": "announce", "data": {...}, "timestamp": datetime}}
PENDING_CONFIRMATIONS = {}


def clear_expired_confirmations():
    """
    Remove expired confirmation requests from the pending confirmations store.

    Iterates through all pending confirmations and removes any that have
    exceeded the CONFIRMATION_TIMEOUT_MINUTES threshold.
    """
    current_time = datetime.now(timezone.utc)
    expired_users = []

    for user_id, confirmation in PENDING_CONFIRMATIONS.items():
        if (current_time - confirmation["timestamp"]).total_seconds() > (
            CONFIRMATION_TIMEOUT_MINUTES * 60
        ):
            expired_users.append(user_id)

    for user_id in expired_users:
        del PENDING_CONFIRMATIONS[user_id]
        logger.info(f"CONFIRMATION: Expired confirmation for user {user_id}")


def add_pending_confirmation(user_id, action_type, data):
    """
    Add a pending confirmation for a user.

    Args:
        user_id: Slack user ID requesting the action
        action_type: Type of action awaiting confirmation (e.g., "announce", "remind")
        data: Dictionary containing action-specific data to store
    """
    clear_expired_confirmations()  # Clean up first
    PENDING_CONFIRMATIONS[user_id] = {
        "action": action_type,
        "data": data,
        "timestamp": datetime.now(timezone.utc),
    }
    logger.info(f"CONFIRMATION: Added pending {action_type} confirmation for user {user_id}")


def get_pending_confirmation(user_id):
    """
    Get pending confirmation for a user.

    Args:
        user_id: Slack user ID to look up

    Returns:
        Dict with keys 'action', 'data', 'timestamp' if confirmation exists, None otherwise
    """
    clear_expired_confirmations()  # Clean up first
    return PENDING_CONFIRMATIONS.get(user_id)


def remove_pending_confirmation(user_id):
    """
    Remove pending confirmation for a user.

    Args:
        user_id: Slack user ID whose confirmation should be removed
    """
    if user_id in PENDING_CONFIRMATIONS:
        action = PENDING_CONFIRMATIONS[user_id]["action"]
        del PENDING_CONFIRMATIONS[user_id]
        logger.info(f"CONFIRMATION: Removed pending {action} confirmation for user {user_id}")


def handle_confirm_command(user_id, say, app):
    """
    Handle confirmation of pending mass notification commands.

    Executes the pending action (announce/remind) if a valid confirmation exists
    for the user and it hasn't expired.

    Args:
        user_id: Slack user ID confirming the action
        say: Slack say function for sending messages
        app: Slack app instance
    """
    username = get_username(app, user_id)

    # Check if there's a pending confirmation for this user
    confirmation = get_pending_confirmation(user_id)
    if not confirmation:
        say("No pending confirmation found. Confirmations expire after 5 minutes.")
        logger.info(
            f"CONFIRMATION: {username} ({user_id}) attempted to confirm but no pending confirmation found"
        )
        return

    action_type = confirmation["action"]
    action_data = confirmation["data"]

    logger.info(f"CONFIRMATION: {username} ({user_id}) confirming {action_type} action")

    try:
        if action_type == "announce":
            # Execute the announcement
            from services.birthday import send_channel_announcement

            announcement_type = action_data["type"]
            custom_message = action_data.get("message")

            success = send_channel_announcement(app, announcement_type, custom_message)

            from slack.blocks import build_announce_result_blocks

            blocks, fallback = build_announce_result_blocks(success)
            say(blocks=blocks, text=fallback)

            if success:
                logger.info(
                    f"CONFIRMATION: Successfully executed {announcement_type} announcement for {username} ({user_id})"
                )
            else:
                logger.error(
                    f"CONFIRMATION: Failed to execute {announcement_type} announcement for {username} ({user_id})"
                )

        elif action_type == "remind":
            # Execute the reminder
            reminder_type = action_data["type"]
            users = action_data["users"]
            custom_message = action_data.get("message")

            results = send_reminder_to_users(app, users, custom_message, reminder_type)

            # Report results
            successful = results["successful"]
            failed = results["failed"]
            skipped_bots = results["skipped_bots"]
            skipped_inactive = results.get("skipped_inactive", 0)

            from slack.blocks import build_remind_result_blocks

            blocks, fallback = build_remind_result_blocks(
                successful=successful,
                failed=failed,
                skipped_bots=skipped_bots,
                skipped_inactive=skipped_inactive,
            )
            say(blocks=blocks, text=fallback)

            logger.info(
                f"CONFIRMATION: Successfully executed {reminder_type} reminders for {username} ({user_id}) - {successful} sent, {failed} failed"
            )

        else:
            say(f"❌ Unknown action type: {action_type}")
            logger.error(
                f"CONFIRMATION: Unknown action type {action_type} for {username} ({user_id})"
            )

    except Exception as e:
        say(f"❌ Error executing confirmation: {e}")
        logger.error(f"CONFIRMATION: Error executing {action_type} for {username} ({user_id}): {e}")

    finally:
        # Always remove the pending confirmation
        remove_pending_confirmation(user_id)


def handle_dm_help(say):
    """
    Send help information for DM commands.

    Displays available commands for regular users in Block Kit format.

    Args:
        say: Slack say function for sending messages
    """
    from slack.blocks import build_help_blocks

    blocks, fallback = build_help_blocks(is_admin=False)
    say(blocks=blocks, text=fallback)
    logger.info("HELP: Sent DM help information")


def handle_dm_admin_help(say, user_id, app):
    """
    Send admin help information using fully structured Block Kit.

    Checks admin permission before displaying admin-specific commands.
    Shows permission error if user is not an admin.

    Args:
        say: Slack say function for sending messages
        user_id: Slack user ID requesting admin help
        app: Slack app instance for permission checking
    """
    if not is_admin(app, user_id):
        from slack.blocks import build_permission_error_blocks

        blocks, fallback = build_permission_error_blocks("admin help", "admin")
        say(blocks=blocks, text=fallback)
        return

    from slack.blocks import build_help_blocks

    blocks, fallback = build_help_blocks(is_admin=True)
    say(blocks=blocks, text=fallback)
    logger.info(f"HELP: Sent admin help to {user_id}")


def handle_dm_date(say, user, result, app):
    """
    Handle a date sent in a DM to set or update user's birthday.

    Parses the date result, saves the birthday, and sends appropriate
    confirmation. Triggers immediate celebration if birthday is today.

    Args:
        say: Slack say function for sending messages
        user: Slack user ID who sent the date
        result: Dict from extract_date() with keys 'date', 'year', 'status'
        app: Slack app instance
    """
    date = result["date"]
    year = result["year"]

    # Format birthday information for response
    if year:
        date_words = date_to_words(date, year)
        age = calculate_age(year)
        age_text = f" (Age: {age})"
    else:
        date_words = date_to_words(date)
        age_text = ""

    username = get_username(app, user)
    updated = save_birthday(date, user, year, username)

    # Check if birthday is today and send announcement if so
    if check_if_birthday_today(date):
        send_immediate_birthday_announcement(
            user, username, date, year, date_words, age_text, say, app
        )
    else:
        # Enhanced confirmation messages with Block Kit
        try:
            from slack.blocks import build_confirmation_blocks

            # Get star sign and celebration style for confirmation
            star_sign = get_star_sign(date)
            prefs = get_user_preferences(user) or {}
            celebration_style = prefs.get("celebration_style", "standard")
            style_emoji = CELEBRATION_STYLE_EMOJIS.get(celebration_style, "🎊")
            style_desc = CELEBRATION_STYLES.get(celebration_style, "Standard celebration")

            # Build details dict
            details = {
                "📅 Birthday": date_words,
                "⭐ Star Sign": star_sign,
            }
            if age_text:
                details["🎈 Age"] = age_text.replace(" (Age: ", "").replace(")", "")
            details[f"{style_emoji} Style"] = f"{celebration_style.title()} - {style_desc}"

            if updated:
                blocks, fallback = build_confirmation_blocks(
                    title="Birthday Updated!",
                    message="Your birthday has been updated successfully.\n\nIf this is incorrect, please send the correct date.",
                    action_type="success",
                    details=details,
                )
                say(blocks=blocks, text=fallback)
                logger.info(
                    f"BIRTHDAY_UPDATE: Successfully notified {username} ({user}) of birthday update to {date_words} via date input"
                )
            else:
                blocks, fallback = build_confirmation_blocks(
                    title="Birthday Saved!",
                    message="Your birthday has been saved successfully!\n\nIf this is incorrect, please send the correct date.",
                    action_type="success",
                    details=details,
                )
                say(blocks=blocks, text=fallback)
                logger.info(
                    f"BIRTHDAY_ADD: Successfully notified {username} ({user}) of new birthday {date_words} via date input"
                )
        except Exception as e:
            logger.error(
                f"NOTIFICATION_ERROR: Failed to send birthday confirmation to {username} ({user}) via date input: {e}"
            )
            # Fallback to simple message without formatting
            try:
                if updated:
                    say(
                        f"Birthday updated to {date_words}{age_text}. If this is incorrect, please try again with the correct date."
                    )
                else:
                    say(
                        f"{date_words}{age_text} has been saved as your birthday. If this is incorrect, please try again."
                    )
                logger.info(
                    f"BIRTHDAY_FALLBACK: Sent fallback confirmation to {username} ({user}) via date input"
                )
            except Exception as fallback_error:
                logger.error(
                    f"NOTIFICATION_CRITICAL: Complete failure to notify {username} ({user}) via date input: {fallback_error}"
                )

    # Send external backup after user confirmation to avoid API conflicts
    _send_external_backup_if_enabled(updated, username, app, user_id=user)


def _send_external_backup_if_enabled(updated, username, app, change_type=None, user_id=None):
    """
    Send external backup after birthday changes if enabled.

    Args:
        updated: Whether this was an update (True) or new addition (False)
        username: Username of the person whose birthday changed
        app: Slack app instance for sending backup
        change_type: Optional override for change type ("add", "update", "remove")
        user_id: User ID of the person whose birthday changed (for preferences lookup)
    """
    from storage.birthdays import trigger_external_backup

    trigger_external_backup(updated, username, app, change_type, user_id)


def handle_command(text, user_id, say, app):
    """Process commands sent as direct messages"""
    parts = text.strip().lower().split()
    command = parts[0] if parts else "help"
    username = get_username(app, user_id)

    logger.info(f"COMMAND: {username} ({user_id}) used DM command: {text}")

    if command == "help":
        handle_dm_help(say)
        return

    if command == "admin" and len(parts) > 1:
        admin_subcommand = parts[1]

        if admin_subcommand == "help":
            handle_dm_admin_help(say, user_id, app)
            return

        if not is_admin(app, user_id):
            from slack.blocks import build_permission_error_blocks

            blocks, fallback = build_permission_error_blocks("admin commands", "admin")
            say(blocks=blocks, text=fallback)
            logger.warning(
                f"PERMISSIONS: {username} ({user_id}) attempted to use admin command without permission"
            )
            return

        # Special handling for admin special commands that need quoted string parsing
        if admin_subcommand == "special":
            # Pass the original text after "admin special" for quoted parsing
            admin_special_text = text[len("admin special") :].strip()
            handle_admin_special_command_with_quotes(admin_special_text, user_id, say, app)
        else:
            handle_admin_command(
                admin_subcommand,
                parts[2:],
                say,
                user_id,
                app,
                add_pending_confirmation,
                CONFIRMATION_TIMEOUT_MINUTES,
            )
        return

    if command == "add" and len(parts) >= 2:
        _handle_add_command(parts, user_id, username, say, app)

    elif command == "remove":
        _handle_remove_command(user_id, username, say, app)

    elif command == "pause":
        _handle_pause_command(user_id, say)

    elif command == "resume":
        _handle_resume_command(user_id, say)

    elif command == "list":
        handle_list_command(parts, user_id, say, app)

    elif command == "check":
        handle_check_command(parts, user_id, say, app)

    elif command == "remind":
        handle_remind_command(
            parts,
            user_id,
            say,
            app,
            add_pending_confirmation,
            CONFIRMATION_TIMEOUT_MINUTES,
        )

    elif command == "stats":
        handle_stats_command(user_id, say, app)

    elif command == "config":
        handle_config_command(parts, user_id, say, app)

    elif command == "test":
        quality, image_size, text_only, error_message = parse_test_command_args(parts[1:])
        if error_message:
            say(error_message)
            return
        handle_test_command(user_id, say, app, quality, image_size, text_only=text_only)

    elif command == "special":
        handle_special_command(parts[1:] if len(parts) > 1 else [], user_id, say, app)

    elif command == "hello":
        _handle_hello_command(user_id, say)

    elif command == "confirm":
        handle_confirm_command(user_id, say, app)

    else:
        # Unknown command
        handle_dm_help(say)


def _handle_add_command(parts, user_id, username, say, app):
    """
    Handle the add birthday command.

    Parses date from command parts, validates it, and saves the birthday.
    Sends appropriate confirmation or error messages via Block Kit.

    Args:
        parts: List of command parts (e.g., ["add", "25/12", "1990"])
        user_id: Slack user ID adding their birthday
        username: Display name of the user
        say: Slack say function for sending messages
        app: Slack app instance
    """
    date_text = " ".join(parts[1:])
    result = extract_date(date_text)

    if result["status"] == "no_date":
        from slack.blocks import build_birthday_error_blocks

        blocks, fallback = build_birthday_error_blocks("no_date")
        say(blocks=blocks, text=fallback)
        return

    if result["status"] == "invalid_date":
        from slack.blocks import build_birthday_error_blocks

        blocks, fallback = build_birthday_error_blocks("invalid_date")
        say(blocks=blocks, text=fallback)
        return

    date = result["date"]
    year = result["year"]

    updated = save_birthday(date, user_id, year, username)

    if year:
        date_words = date_to_words(date, year)
        age = calculate_age(year)
        age_text = f" (Age: {age})"
    else:
        date_words = date_to_words(date)
        age_text = ""

    # Check if birthday is today and send announcement if so
    if check_if_birthday_today(date):
        send_immediate_birthday_announcement(
            user_id, username, date, year, date_words, age_text, say, app
        )
    else:
        # Enhanced confirmation messages with Block Kit
        try:
            from slack.blocks import build_confirmation_blocks

            # Get star sign and celebration style for confirmation
            star_sign = get_star_sign(date)
            prefs = get_user_preferences(user_id) or {}
            celebration_style = prefs.get("celebration_style", "standard")
            style_emoji = CELEBRATION_STYLE_EMOJIS.get(celebration_style, "🎊")
            style_desc = CELEBRATION_STYLES.get(celebration_style, "Standard celebration")

            # Build details dict
            details = {
                "📅 Birthday": date_words,
                "⭐ Star Sign": star_sign,
            }
            if age_text:
                details["🎈 Age"] = age_text.replace(" (Age: ", "").replace(")", "")
            details[f"{style_emoji} Style"] = f"{celebration_style.title()} - {style_desc}"

            if updated:
                blocks, fallback = build_confirmation_blocks(
                    title="Birthday Updated!",
                    message="Your birthday has been updated successfully.",
                    action_type="success",
                    details=details,
                )
                say(blocks=blocks, text=fallback)
                logger.info(
                    f"BIRTHDAY_UPDATE: Successfully notified {username} ({user_id}) of birthday update to {date_words}"
                )
            else:
                blocks, fallback = build_confirmation_blocks(
                    title="Birthday Saved!",
                    message="Your birthday has been saved successfully!",
                    action_type="success",
                    details=details,
                )
                say(blocks=blocks, text=fallback)
                logger.info(
                    f"BIRTHDAY_ADD: Successfully notified {username} ({user_id}) of new birthday {date_words}"
                )
        except Exception as e:
            logger.error(
                f"NOTIFICATION_ERROR: Failed to send birthday confirmation to {username} ({user_id}): {e}"
            )
            # Fallback to simple message without formatting
            try:
                if updated:
                    say(f"Your birthday has been updated to {date_words}{age_text}")
                else:
                    say(f"Your birthday ({date_words}{age_text}) has been saved!")
                logger.info(
                    f"BIRTHDAY_FALLBACK: Sent fallback confirmation to {username} ({user_id})"
                )
            except Exception as fallback_error:
                logger.error(
                    f"NOTIFICATION_CRITICAL: Complete failure to notify {username} ({user_id}): {fallback_error}"
                )

    # Send external backup after user confirmation
    _send_external_backup_if_enabled(updated, username, app, user_id=user_id)


def _handle_remove_command(user_id, username, say, app):
    """
    Handle the remove birthday command.

    Removes the user's birthday from storage and sends confirmation.

    Args:
        user_id: Slack user ID requesting removal
        username: Display name of the user
        say: Slack say function for sending messages
        app: Slack app instance
    """
    removed = remove_birthday(user_id, username)

    try:
        from slack.blocks import build_confirmation_blocks

        if removed:
            blocks, fallback = build_confirmation_blocks(
                title="Birthday Removed",
                message="Your birthday has been successfully removed from our records.",
                action_type="success",
            )
            say(blocks=blocks, text=fallback)
            logger.info(
                f"BIRTHDAY_REMOVE: Successfully notified {username} ({user_id}) of birthday removal"
            )
        else:
            blocks, fallback = build_confirmation_blocks(
                title="No Birthday Found",
                message="You don't currently have a birthday saved in our records.\n\nUse `add DD/MM` or `add DD/MM/YYYY` to save your birthday.",
                action_type="info",
            )
            say(blocks=blocks, text=fallback)
            logger.info(
                f"BIRTHDAY_REMOVE: Notified {username} ({user_id}) that no birthday was found to remove"
            )
    except Exception as e:
        logger.error(
            f"NOTIFICATION_ERROR: Failed to send birthday removal confirmation to {username} ({user_id}): {e}"
        )
        # Fallback to simple message without formatting
        try:
            if removed:
                say("Your birthday has been removed from our records")
            else:
                say("You don't have a birthday saved in our records")
            logger.info(
                f"BIRTHDAY_REMOVE_FALLBACK: Sent fallback confirmation to {username} ({user_id})"
            )
        except Exception as fallback_error:
            logger.error(
                f"NOTIFICATION_CRITICAL: Complete failure to notify {username} ({user_id}) about removal: {fallback_error}"
            )

    # Send external backup after user confirmation (only if birthday was actually removed)
    if removed:
        _send_external_backup_if_enabled(True, username, app, change_type="remove")


def _handle_pause_command(user_id, say):
    """
    Handle pause command via DM.

    Pauses birthday celebrations for the user.

    Args:
        user_id: Slack user ID
        say: Slack say function for sending messages
    """
    from storage.birthdays import get_birthday, update_user_preferences

    birthday = get_birthday(user_id)
    if not birthday:
        say("You haven't added your birthday yet. Use `add DD/MM` to add it first.")
        return

    # Update preferences to pause
    success = update_user_preferences(user_id, {"active": False})

    if success:
        logger.info(f"PAUSE: User {user_id} paused their birthday celebrations")
        say(
            "Your birthday celebrations have been paused. You won't receive any announcements until you resume. Use `resume` to enable again."
        )
    else:
        say("Unable to pause celebrations. Please try again.")


def _handle_resume_command(user_id, say):
    """
    Handle resume command via DM.

    Resumes birthday celebrations for the user.

    Args:
        user_id: Slack user ID
        say: Slack say function for sending messages
    """
    from storage.birthdays import get_birthday, update_user_preferences

    birthday = get_birthday(user_id)
    if not birthday:
        say("You haven't added your birthday yet. Use `add DD/MM` to add it first.")
        return

    # Update preferences to resume
    success = update_user_preferences(user_id, {"active": True})

    if success:
        logger.info(f"RESUME: User {user_id} resumed their birthday celebrations")
        say(
            "Your birthday celebrations have been resumed! You'll receive announcements on your birthday."
        )
    else:
        say("Unable to resume celebrations. Please try again.")


def _handle_hello_command(user_id, say):
    """
    Handle the hello greeting command.

    Sends a personality-specific greeting to the user using Block Kit.

    Args:
        user_id: Slack user ID to greet
        say: Slack say function for sending messages
    """
    current_personality = get_current_personality_name()

    # Handle random personality by selecting a specific one
    if current_personality == "random":
        selected_personality = get_random_personality_name()
        personality_config = get_personality_config(selected_personality)
    else:
        personality_config = get_personality_config(current_personality)

    # Get greeting from personality config and format with user mention
    greeting_template = personality_config.get("hello_greeting", "Hello {user_mention}! 👋")
    greeting = greeting_template.format(user_mention=get_user_mention(user_id))

    # Build Block Kit hello message
    from slack.blocks import build_hello_blocks

    personality_display_name = personality_config.get("name", "BrightDay")
    blocks, fallback = build_hello_blocks(greeting, personality_display_name)

    say(blocks=blocks, text=fallback)
    logger.info(f"HELLO: Sent greeting to user ({user_id}) with {current_personality} personality")


def handle_admin_command(
    subcommand, args, say, user_id, app, add_pending_fn=None, timeout_minutes=None
):
    """
    Handle admin-specific commands by routing to appropriate handler.

    Args:
        subcommand: Admin subcommand (e.g., "list", "add", "backup", "status")
        args: List of additional arguments for the subcommand
        say: Slack say function for sending messages
        user_id: Slack user ID of admin executing command
        app: Slack app instance
        add_pending_fn: Optional custom function for adding pending confirmations
        timeout_minutes: Optional custom timeout for confirmations
    """
    username = get_username(app, user_id)

    # Use provided confirmation functions or fall back to module-level ones
    if add_pending_fn is None:
        add_pending_fn = add_pending_confirmation
    if timeout_minutes is None:
        timeout_minutes = CONFIRMATION_TIMEOUT_MINUTES

    if subcommand == "list":
        handle_admin_list_command(args, user_id, say, app, username)

    elif subcommand == "add" and args:
        handle_admin_add_command(args, user_id, say, app, username)

    elif subcommand == "remove" and args:
        handle_admin_remove_command(args, user_id, say, app, username)

    elif subcommand == "backup":
        handle_backup_command(args, user_id, say, app, username)

    elif subcommand == "restore":
        handle_restore_command(args, user_id, say, app, username)

    elif subcommand == "personality":
        handle_personality_command(args, user_id, say, app, username)

    elif subcommand == "model":
        handle_model_command(args, user_id, say, app, username)

    elif subcommand == "cache":
        handle_cache_command(args, user_id, say, app)

    elif subcommand == "status":
        is_detailed = len(args) > 0 and args[0].lower() == "detailed"
        handle_status_command([None, "detailed" if is_detailed else None], user_id, say, app)
        logger.info(
            f"ADMIN: {username} ({user_id}) requested system status {'with details' if is_detailed else ''}"
        )

    elif subcommand == "timezone":
        handle_timezone_command(args, user_id, say, app, username)

    elif subcommand == "test-block":
        handle_test_block_command(user_id, args, say, app)

    elif subcommand == "test-upload":
        handle_test_upload_command(user_id, say, app)

    elif subcommand == "test-upload-multi":
        handle_test_upload_multi_command(user_id, say, app)

    elif subcommand == "test-blockkit":
        handle_test_blockkit_command(user_id, args, say, app)

    elif subcommand == "test-file-upload":
        handle_test_file_upload_command(user_id, say, app)

    elif subcommand == "test-external-backup":
        handle_test_external_backup_command(user_id, say, app)

    elif subcommand == "test":
        handle_test_birthday_command(args, user_id, say, app)

    elif subcommand == "test-join":
        handle_test_join_command(args, user_id, say, app)

    elif subcommand == "announce":
        handle_announce_command(args, user_id, say, app, add_pending_fn, timeout_minutes)

    elif subcommand == "test-bot-celebration":
        quality, image_size, text_only, error_message = parse_test_command_args(args)
        if error_message:
            say(error_message)
            return
        handle_test_bot_celebration_command(
            user_id, say, app, quality, image_size, text_only=text_only
        )

    elif subcommand == "special":
        handle_admin_special_command(args, user_id, say, app)

    else:
        say("Unknown admin command. Use `admin help` for information on admin commands.")
