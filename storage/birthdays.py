"""
JSON-based data storage and backup management for BrightDayBot.

Handles birthday data persistence with user preferences, automatic backups,
announcement tracking, and external backup delivery with file locking.

Storage format:
{
  "USER_ID": {
    "date": "DD/MM",
    "year": YYYY or null,
    "preferences": {
      "active": true,
      "image_enabled": true,
      "show_age": true
    },
    "created_at": "ISO timestamp",
    "updated_at": "ISO timestamp"
  }
}

Key functions: load_birthdays(), save_birthday(), get_user_preferences(), update_user_preferences()
"""

import json
import os
import shutil
import threading
from datetime import datetime, timezone

from filelock import FileLock

from config import (
    ANNOUNCEMENT_RETENTION_DAYS,
    ANNOUNCEMENTS_FILE,
    BACKUP_CHANNEL_ID,
    BACKUP_DIR,
    BACKUP_TO_ADMINS,
    BIRTHDAYS_JSON_FILE,
    EXTERNAL_BACKUP_ENABLED,
    MAX_BACKUPS,
    TIMEOUTS,
    get_logger,
)
from slack.client import send_message_with_file
from storage.settings import get_current_admins

logger = get_logger("storage")

# File lock for birthday data operations (cross-process)
BIRTHDAYS_LOCK_FILE = BIRTHDAYS_JSON_FILE + ".lock"

# File lock for announcements tracking
ANNOUNCEMENTS_LOCK_FILE = ANNOUNCEMENTS_FILE + ".lock"

# Thread lock for atomic read-modify-write operations (same process)
_birthdays_thread_lock = threading.Lock()

# Default preferences for new users
DEFAULT_PREFERENCES = {
    "active": True,
    "image_enabled": True,
    "show_age": True,
    "celebration_style": "standard",  # Options: "quiet", "standard", "epic"
}

# Valid celebration styles with descriptions
CELEBRATION_STYLES = {
    "quiet": "Simple message only, no AI image",
    "standard": "Message with AI-generated birthday image",
    "epic": "Over-the-top message, AI image, and celebratory reactions",
}

# Celebration style emojis for display
CELEBRATION_STYLE_EMOJIS = {
    "quiet": "🤫",
    "standard": "🎊",
    "epic": "🚀",
}


def create_backup():
    """
    Create a timestamped backup of the birthdays JSON file.

    Returns:
        str: Path to created backup file, or None if backup failed
    """
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
        logger.info(f"BACKUP: Created backup directory at {BACKUP_DIR}")

    if not os.path.exists(BIRTHDAYS_JSON_FILE):
        logger.warning(f"BACKUP: Cannot backup {BIRTHDAYS_JSON_FILE} as it does not exist")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_DIR, f"birthdays_{timestamp}.json")

    try:
        shutil.copy2(BIRTHDAYS_JSON_FILE, backup_file)
        logger.info(f"BACKUP: Created backup at {backup_file}")
        rotate_backups()
        return backup_file

    except OSError as e:
        logger.error(f"BACKUP_ERROR: Failed to create backup: {e}")
        return None


def rotate_backups():
    """
    Maintain only the specified number of most recent JSON backups.
    """
    try:
        backup_files = [
            os.path.join(BACKUP_DIR, f)
            for f in os.listdir(BACKUP_DIR)
            if f.startswith("birthdays_") and f.endswith(".json")
        ]

        backup_files.sort(key=lambda x: os.path.getmtime(x))

        while len(backup_files) > MAX_BACKUPS:
            oldest = backup_files.pop(0)
            os.remove(oldest)
            logger.info(f"BACKUP: Removed old backup {oldest}")

    except OSError as e:
        logger.error(f"BACKUP_ERROR: Failed to rotate backups: {e}")


def send_external_backup(
    backup_file_path, change_type="update", username=None, app=None, user_id=None
):
    """
    Send backup file to admin users via DM and optionally to backup channel.

    Args:
        backup_file_path: Path to the backup file to send
        change_type: Type of change that triggered backup ("add", "update", "remove", "manual")
        username: Username of person whose birthday changed (for context)
        app: Slack app instance (required for sending messages)
        user_id: User ID of person whose birthday changed (for preferences lookup)
    """
    logger.info(f"BACKUP: send_external_backup called - type: {change_type}, user: {username}")

    if not EXTERNAL_BACKUP_ENABLED or not app:
        logger.debug("BACKUP: External backup disabled or no app instance")
        return

    try:
        if not os.path.exists(backup_file_path):
            logger.error(f"BACKUP: Backup file not found: {backup_file_path}")
            return

        file_size = os.path.getsize(backup_file_path)
        file_size_kb = round(file_size / 1024, 1)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        birthdays = load_birthdays()
        total_birthdays = len(birthdays)

        change_text = {
            "add": f"Added birthday for {username}" if username else "Added birthday",
            "update": f"Updated birthday for {username}" if username else "Updated birthday",
            "remove": f"Removed birthday for {username}" if username else "Removed birthday",
            "manual": "Manual backup created",
        }.get(change_type, "Data changed")

        # Get user's preferences for context if available
        prefs_text = ""
        if user_id and change_type in ("add", "update"):
            user_data = birthdays.get(user_id, {})
            prefs = user_data.get("preferences", {})
            style = prefs.get("celebration_style", "standard")
            if style != "standard":
                style_emoji = CELEBRATION_STYLE_EMOJIS.get(style, "🎊")
                style_desc = CELEBRATION_STYLES.get(style, "")
                prefs_text = f"\n{style_emoji} *Style:* {style.title()} - {style_desc}"

        message = f"""🗂️ *Birthday Data Backup* - {timestamp}

📊 *Changes:* {change_text}{prefs_text}
📁 *File:* {os.path.basename(backup_file_path)} ({file_size_kb} KB)
👥 *Total Birthdays:* {total_birthdays} people
🔄 *Auto-backup after data changes*

This backup was automatically created to protect your birthday data."""

        if BACKUP_TO_ADMINS:
            current_admin_users = get_current_admins()
            if not current_admin_users:
                logger.warning(
                    "BACKUP: No bot admins configured - external backup will not be sent."
                )
                return

            success_count = 0
            logger.info(
                f"BACKUP: Starting external backup delivery to {len(current_admin_users)} admin(s)"
            )
            for admin_id in current_admin_users:
                try:
                    if send_message_with_file(app, admin_id, message, backup_file_path):
                        success_count += 1
                        logger.info(f"BACKUP: Successfully sent backup to admin {admin_id}")
                    else:
                        logger.error(f"BACKUP: Failed to send backup to admin {admin_id}")
                except Exception as e:
                    logger.error(f"BACKUP: Error sending to admin {admin_id}: {e}")

            logger.info(
                f"BACKUP: Sent external backup to {success_count}/{len(current_admin_users)} admins"
            )

        if BACKUP_CHANNEL_ID:
            try:
                if send_message_with_file(app, BACKUP_CHANNEL_ID, message, backup_file_path):
                    logger.info(f"BACKUP: Sent backup to channel {BACKUP_CHANNEL_ID}")
                else:
                    logger.warning(f"BACKUP: Failed to send backup to channel {BACKUP_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"BACKUP: Error sending to backup channel: {e}")

    except Exception as e:
        logger.error(f"BACKUP: Failed to send external backup: {e}")


def trigger_external_backup(updated, username, app, change_type=None, user_id=None):
    """
    Trigger external backup after birthday changes if enabled.

    Finds the latest backup file and sends it to admins/backup channel.

    Args:
        updated: Whether this was an update (True) or new addition (False)
        username: Username of the person whose birthday changed
        app: Slack app instance for sending backup
        change_type: Optional override for change type ("add", "update", "remove")
        user_id: User ID of the person whose birthday changed (for preferences lookup)
    """
    from config import BACKUP_ON_EVERY_CHANGE

    try:
        if not EXTERNAL_BACKUP_ENABLED or not BACKUP_ON_EVERY_CHANGE:
            return

        backup_files = [
            os.path.join(BACKUP_DIR, f)
            for f in os.listdir(BACKUP_DIR)
            if f.startswith("birthdays_") and f.endswith(".json")
        ]
        if backup_files:
            latest_backup = max(backup_files, key=lambda x: os.path.getmtime(x))
            if change_type is None:
                change_type = "update" if updated else "add"
            send_external_backup(latest_backup, change_type, username, app, user_id)
    except Exception as e:
        logger.error(f"BACKUP: Failed to trigger external backup: {e}")


def restore_latest_backup():
    """
    Restore the most recent JSON backup file.

    Returns:
        bool: True if restore succeeded, False otherwise
    """
    try:
        backup_files = [
            os.path.join(BACKUP_DIR, f)
            for f in os.listdir(BACKUP_DIR)
            if f.startswith("birthdays_") and f.endswith(".json")
        ]

        if not backup_files:
            logger.warning("RESTORE: No backup files found")
            return False

        backup_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        latest = backup_files[0]

        shutil.copy2(latest, BIRTHDAYS_JSON_FILE)
        logger.info(f"RESTORE: Successfully restored from {latest}")
        return True

    except OSError as e:
        logger.error(f"RESTORE_ERROR: Failed to restore from backup: {e}")
        return False


def load_birthdays():
    """
    Load birthdays from JSON storage.

    Returns:
        Dictionary mapping user_id to birthday data with preferences
    """
    lock = FileLock(BIRTHDAYS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])

    try:
        with lock:
            with open(BIRTHDAYS_JSON_FILE, "r") as f:
                data = json.load(f)
                logger.info(f"STORAGE: Loaded {len(data)} birthdays from JSON")
                return data
    except FileNotFoundError:
        logger.warning(f"FILE_ERROR: {BIRTHDAYS_JSON_FILE} not found")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"JSON_ERROR: Failed to parse birthdays JSON: {e}")
        return {}
    except PermissionError as e:
        logger.error(f"PERMISSION_ERROR: Cannot read {BIRTHDAYS_JSON_FILE}: {e}")
        return {}
    except Exception as e:
        logger.error(f"UNEXPECTED_ERROR: Failed to load birthdays: {e}")
        return {}


def save_birthdays(birthdays):
    """
    Save birthdays dictionary to JSON storage.

    Args:
        birthdays: Dictionary mapping user_id to birthday data with preferences
    """
    lock = FileLock(BIRTHDAYS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])

    try:
        with lock:
            with open(BIRTHDAYS_JSON_FILE, "w") as f:
                json.dump(birthdays, f, indent=2, sort_keys=True)

            logger.info(f"STORAGE: Saved {len(birthdays)} birthdays to JSON")
            create_backup()

    except PermissionError as e:
        logger.error(f"PERMISSION_ERROR: Cannot write to {BIRTHDAYS_JSON_FILE}: {e}")
    except Exception as e:
        logger.error(f"UNEXPECTED_ERROR: Failed to save birthdays: {e}")


def save_birthday(
    date: str, user: str, year: int = None, username: str = None, preferences: dict = None
) -> bool:
    """
    Save user's birthday to the record (thread-safe atomic operation).

    Args:
        date: Date in DD/MM format
        user: User ID
        year: Optional birth year
        username: User's display name (for logging)
        preferences: Optional user preferences dict

    Returns:
        True if updated existing record, False if new record
    """
    # Use thread lock for atomic read-modify-write
    with _birthdays_thread_lock:
        birthdays = load_birthdays()
        updated = user in birthdays
        now = datetime.now(timezone.utc).isoformat()

        action = "Updated" if updated else "Added new"
        username_log = username or user

        # Preserve existing preferences if updating
        existing_prefs = {}
        if updated and "preferences" in birthdays[user]:
            existing_prefs = birthdays[user]["preferences"]

        # Merge with provided preferences or defaults
        merged_prefs = {**DEFAULT_PREFERENCES, **existing_prefs}
        if preferences:
            merged_prefs.update(preferences)

        # Set show_age based on year if not explicitly set
        if "show_age" not in (preferences or {}):
            merged_prefs["show_age"] = year is not None

        birthdays[user] = {
            "date": date,
            "year": year,
            "preferences": merged_prefs,
            "created_at": birthdays.get(user, {}).get("created_at", now),
            "updated_at": now,
        }

        save_birthdays(birthdays)
        logger.info(
            f"BIRTHDAY: {action} birthday for {username_log} ({user}): {date}"
            + (f", year: {year}" if year else "")
        )
        return updated


def remove_birthday(user: str, username: str = None) -> bool:
    """
    Remove user's birthday from the record (thread-safe atomic operation).

    Args:
        user: User ID
        username: User's display name (for logging)

    Returns:
        True if removed, False if not found
    """
    # Use thread lock for atomic read-modify-write
    with _birthdays_thread_lock:
        birthdays = load_birthdays()
        if user in birthdays:
            username_log = username or user
            del birthdays[user]
            save_birthdays(birthdays)
            logger.info(f"BIRTHDAY: Removed birthday for {username_log} ({user})")
            return True

        logger.info(f"BIRTHDAY: Attempted to remove birthday for user {user} but none was found")
        return False


def get_birthday(user: str) -> dict:
    """
    Get a user's birthday data.

    Args:
        user: User ID

    Returns:
        Birthday data dict or None if not found
    """
    birthdays = load_birthdays()
    return birthdays.get(user)


def get_user_preferences(user: str) -> dict:
    """
    Get user's celebration preferences.

    Args:
        user: User ID

    Returns:
        Preferences dict (with defaults if not set), or None if user not found
    """
    birthday_data = get_birthday(user)
    if not birthday_data:
        return None

    return {**DEFAULT_PREFERENCES, **birthday_data.get("preferences", {})}


def update_user_preferences(user: str, preferences: dict) -> bool:
    """
    Update user's celebration preferences (thread-safe atomic operation).

    Args:
        user: User ID
        preferences: Dict with preference keys to update

    Returns:
        True if updated, False if user not found
    """
    # Use thread lock for atomic read-modify-write
    with _birthdays_thread_lock:
        birthdays = load_birthdays()
        if user not in birthdays:
            return False

        now = datetime.now(timezone.utc).isoformat()

        # Merge preferences
        current_prefs = birthdays[user].get("preferences", DEFAULT_PREFERENCES.copy())
        current_prefs.update(preferences)

        birthdays[user]["preferences"] = current_prefs
        birthdays[user]["updated_at"] = now

        save_birthdays(birthdays)
        logger.info(f"PREFERENCES: Updated preferences for user {user}: {preferences}")
        return True


def is_user_active(user: str, birthday_data: dict = None) -> bool:
    """
    Check if user's birthday celebrations are active.

    Args:
        user: User ID
        birthday_data: Optional pre-loaded birthday data to avoid re-fetching

    Returns:
        True if active (or not set), False if paused
    """
    if birthday_data is not None:
        # Use provided data directly to avoid reloading all birthdays
        prefs = {**DEFAULT_PREFERENCES, **birthday_data.get("preferences", {})}
    else:
        prefs = get_user_preferences(user)
        if prefs is None:
            return True  # No birthday = default active
    return prefs.get("active", True)


def get_all_active_birthdays() -> dict:
    """
    Get all birthdays where user is active (not paused).

    Returns:
        Dictionary of active birthday entries
    """
    birthdays = load_birthdays()

    return {
        user_id: data
        for user_id, data in birthdays.items()
        if data.get("preferences", {}).get("active", True)
    }


# ==================== ANNOUNCEMENT TRACKING (Consolidated JSON) ====================


def _load_announcements() -> dict:
    """
    Load announcements tracking data from JSON file.

    Returns:
        Dictionary with structure:
        {
            "birthdays": {"YYYY-MM-DD": ["user_id1", "user_id2"]},
            "timezone_birthdays": {"YYYY-MM-DD": {"user_id": "timezone"}},
            "special_days": {"YYYY-MM-DD": "ISO timestamp"},
            "last_cleanup": "ISO timestamp"
        }
    """
    try:
        lock = FileLock(ANNOUNCEMENTS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
        with lock:
            if os.path.exists(ANNOUNCEMENTS_FILE):
                with open(ANNOUNCEMENTS_FILE, "r") as f:
                    return json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"FILE_ERROR: Failed to load announcements: {e}")

    # Return default structure
    return {
        "birthdays": {},
        "timezone_birthdays": {},
        "special_days": {},
        "last_cleanup": None,
    }


def _save_announcements(data: dict) -> bool:
    """
    Save announcements tracking data to JSON file.

    Args:
        data: Dictionary with announcements tracking data

    Returns:
        True if successful, False otherwise
    """
    try:
        lock = FileLock(ANNOUNCEMENTS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
        with lock:
            with open(ANNOUNCEMENTS_FILE, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
        return True
    except Exception as e:
        logger.error(f"FILE_ERROR: Failed to save announcements: {e}")
        return False


def _cleanup_old_announcements(data: dict) -> dict:
    """
    Remove announcement entries older than ANNOUNCEMENT_RETENTION_DAYS.

    Args:
        data: Announcements data dictionary

    Returns:
        Cleaned data dictionary
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=ANNOUNCEMENT_RETENTION_DAYS)).strftime(
        "%Y-%m-%d"
    )

    # Clean birthdays
    data["birthdays"] = {k: v for k, v in data.get("birthdays", {}).items() if k >= cutoff}

    # Clean timezone birthdays
    data["timezone_birthdays"] = {
        k: v for k, v in data.get("timezone_birthdays", {}).items() if k >= cutoff
    }

    # Clean special days
    data["special_days"] = {k: v for k, v in data.get("special_days", {}).items() if k >= cutoff}

    data["last_cleanup"] = datetime.now(timezone.utc).isoformat()

    return data


def get_announced_birthdays_today():
    """
    Get list of user IDs whose birthdays have already been announced today.

    Returns:
        List of user IDs
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _load_announcements()
    return data.get("birthdays", {}).get(today, [])


def mark_birthday_announced(user_id):
    """
    Mark a user's birthday as announced for today.

    Args:
        user_id: User ID whose birthday was announced
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _load_announcements()

    if "birthdays" not in data:
        data["birthdays"] = {}

    if today not in data["birthdays"]:
        data["birthdays"][today] = []

    if user_id not in data["birthdays"][today]:
        data["birthdays"][today].append(user_id)
        if _save_announcements(data):
            logger.info(f"BIRTHDAY: Marked {user_id}'s birthday as announced")
        else:
            logger.error(f"FILE_ERROR: Failed to mark birthday as announced for {user_id}")


def cleanup_old_announcement_files():
    """
    Clean up old announcement entries.
    """
    data = _load_announcements()

    # Clean up old entries
    data = _cleanup_old_announcements(data)
    _save_announcements(data)

    logger.info("CLEANUP: Cleaned old announcement entries")


def get_timezone_announced_birthdays_today():
    """
    Get list of user IDs who have been announced today via timezone-aware celebrations.

    Returns:
        List of entries in format "user_id:timezone"
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _load_announcements()
    tz_data = data.get("timezone_birthdays", {}).get(today, {})

    # Return in legacy format for backwards compatibility
    return [f"{user_id}:{tz}" for user_id, tz in tz_data.items()]


def mark_timezone_birthday_announced(user_id, user_timezone):
    """
    Mark a user's birthday as announced via timezone-aware celebration.

    Args:
        user_id: User ID whose birthday was announced
        user_timezone: User's timezone where celebration occurred
    """
    from config import DEFAULT_TIMEZONE
    from utils.date import get_timezone_object

    if not get_timezone_object(user_timezone):
        logger.warning(f"TIMEZONE: Invalid timezone '{user_timezone}', using default")
        user_timezone = DEFAULT_TIMEZONE

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _load_announcements()

    if "timezone_birthdays" not in data:
        data["timezone_birthdays"] = {}

    if today not in data["timezone_birthdays"]:
        data["timezone_birthdays"][today] = {}

    data["timezone_birthdays"][today][user_id] = user_timezone

    if _save_announcements(data):
        logger.info(f"TIMEZONE: Marked {user_id}'s birthday as announced in {user_timezone}")
    else:
        logger.error(f"FILE_ERROR: Failed to mark timezone birthday as announced for {user_id}")


def cleanup_timezone_announcement_files():
    """
    Clean up old timezone announcement entries.
    Delegates to cleanup_old_announcement_files() which handles all cleanup.
    """
    cleanup_old_announcement_files()


def is_user_celebrated_today(user_id):
    """
    Check if user has been celebrated today via either legacy or timezone-aware system.

    Args:
        user_id: User ID to check

    Returns:
        True if user has been celebrated today, False otherwise
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _load_announcements()

    # Check regular birthdays
    if user_id in data.get("birthdays", {}).get(today, []):
        return True

    # Check timezone birthdays
    if user_id in data.get("timezone_birthdays", {}).get(today, {}):
        return True

    return False
