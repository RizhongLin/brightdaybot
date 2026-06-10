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
      "show_age": true,
      "celebration_style": "standard"
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
    BACKUP_DIR,
    BIRTHDAYS_JSON_FILE,
    EXTERNAL_BACKUP_ENABLED,
    MAX_BACKUPS,
    OPS_CHANNEL_ID,
    TIMEOUTS,
    get_logger,
)

logger = get_logger("storage")

# File lock for birthday data operations (cross-process)
BIRTHDAYS_LOCK_FILE = BIRTHDAYS_JSON_FILE + ".lock"

# Mtime-keyed cache of the parsed birthdays dict. Read-heavy file (≥150x/day)
# with rare writes; mtime invalidation gives correctness without TTL guesswork.
_birthdays_cache_lock = threading.Lock()
_birthdays_cache: tuple | None = None  # (mtime_or_None, dict)


def _invalidate_birthdays_cache() -> None:
    global _birthdays_cache
    with _birthdays_cache_lock:
        _birthdays_cache = None


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


def _list_backup_files():
    """List all birthday backup JSON files sorted by modification time (oldest first)."""
    return sorted(
        [
            os.path.join(BACKUP_DIR, f)
            for f in os.listdir(BACKUP_DIR)
            if f.startswith("birthdays_") and f.endswith(".json")
        ],
        key=lambda x: os.path.getmtime(x),
    )


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
        backup_files = _list_backup_files()

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
    Notify ops channel canvas dashboard of birthday data changes.

    Args:
        backup_file_path: Path to the backup file (used for validation)
        change_type: Type of change ("add", "update", "remove", "manual")
        username: Username of person whose birthday changed (for context)
        app: Slack app instance (required for canvas updates)
        user_id: User ID of person whose birthday changed
    """
    logger.info(f"BACKUP: send_external_backup called - type: {change_type}, user: {username}")

    if not EXTERNAL_BACKUP_ENABLED or not app:
        logger.debug("BACKUP: External backup disabled or no app instance")
        return

    try:
        if not os.path.exists(backup_file_path):
            logger.error(f"BACKUP: Backup file not found: {backup_file_path}")
            return

        change_text = {
            "add": f"Added birthday for {username}" if username else "Added birthday",
            "update": f"Updated birthday for {username}" if username else "Updated birthday",
            "remove": f"Removed birthday for {username}" if username else "Removed birthday",
            "manual": "Manual backup created",
        }.get(change_type, "Data changed")

        if OPS_CHANNEL_ID:
            try:
                from slack.canvas import record_change, update_canvas_async

                record_change(change_text)
                update_canvas_async(app, reason=f"backup_{change_type}")
                logger.info(f"BACKUP: Triggered canvas update for channel {OPS_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"BACKUP: Error triggering canvas update: {e}")

    except Exception as e:
        logger.error(f"BACKUP: Failed to send external backup: {e}")


def trigger_external_backup(updated, username, app, change_type=None, user_id=None):
    """
    Trigger canvas dashboard update after birthday changes if enabled.

    Finds the latest backup file and notifies the ops channel canvas.

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

        backup_files = _list_backup_files()
        if backup_files:
            latest_backup = backup_files[-1]  # Already sorted oldest-first
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
        backup_files = _list_backup_files()

        if not backup_files:
            logger.warning("RESTORE: No backup files found")
            return False

        latest = backup_files[-1]  # Already sorted oldest-first, last = newest

        shutil.copy2(latest, BIRTHDAYS_JSON_FILE)
        logger.info(f"RESTORE: Successfully restored from {latest}")
        return True

    except OSError as e:
        logger.error(f"RESTORE_ERROR: Failed to restore from backup: {e}")
        return False


def _recover_corrupt_birthdays_file():
    """
    Quarantine a corrupt birthdays file and restore the latest backup.

    The corrupt file is renamed aside (birthdays.json.corrupt-<timestamp>) so
    forensics survive and the restored backup isn't immediately re-clobbered.
    Posts a warning to the ops canvas either way.

    Returns:
        dict: Restored birthday data, or {} if no backup could be restored
    """
    lock = FileLock(BIRTHDAYS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
    data: dict = {}

    try:
        with lock:
            try:
                quarantine_path = (
                    f"{BIRTHDAYS_JSON_FILE}.corrupt-" f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                shutil.move(BIRTHDAYS_JSON_FILE, quarantine_path)
                logger.error(f"CORRUPTION: Quarantined corrupt birthdays file to {quarantine_path}")
            except OSError as e:
                logger.error(f"CORRUPTION: Failed to quarantine corrupt file: {e}")

            if restore_latest_backup():
                try:
                    with open(BIRTHDAYS_JSON_FILE, "r") as f:
                        data = json.load(f)
                    logger.warning(
                        f"CORRUPTION: Auto-restored {len(data)} birthdays from latest backup"
                    )
                except (OSError, json.JSONDecodeError) as e:
                    logger.error(f"CORRUPTION: Restored backup is unreadable: {e}")
                    data = {}
    except Exception as e:
        logger.error(f"CORRUPTION: Auto-restore failed: {e}")

    try:
        # Local import to avoid circular dependency (canvas imports storage)
        from slack.canvas import safe_record_warning

        if data:
            safe_record_warning("birthdays.json was corrupt — auto-restored from latest backup")
        else:
            safe_record_warning(
                "birthdays.json is corrupt and no backup could be restored — "
                "birthday data is currently empty"
            )
    except Exception:
        pass

    return data


def load_birthdays():
    """
    Load birthdays from JSON storage (memoized by file mtime).

    On JSON corruption, automatically quarantines the corrupt file and
    restores the latest backup.

    Returns:
        Dictionary mapping user_id to birthday data with preferences
    """
    global _birthdays_cache

    try:
        mtime = os.path.getmtime(BIRTHDAYS_JSON_FILE)
    except OSError:
        mtime = None

    with _birthdays_cache_lock:
        if _birthdays_cache is not None and _birthdays_cache[0] == mtime:
            return _birthdays_cache[1]

    lock = FileLock(BIRTHDAYS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
    data: dict = {}

    try:
        with lock:
            with open(BIRTHDAYS_JSON_FILE, "r") as f:
                data = json.load(f)
                logger.info(f"STORAGE: Loaded {len(data)} birthdays from JSON")
    except FileNotFoundError:
        logger.warning(f"FILE_ERROR: {BIRTHDAYS_JSON_FILE} not found")
    except json.JSONDecodeError as e:
        logger.error(f"JSON_ERROR: Failed to parse birthdays JSON: {e}")
        data = _recover_corrupt_birthdays_file()
        try:
            mtime = os.path.getmtime(BIRTHDAYS_JSON_FILE)
        except OSError:
            mtime = None
    except PermissionError as e:
        logger.error(f"PERMISSION_ERROR: Cannot read {BIRTHDAYS_JSON_FILE}: {e}")
    except Exception as e:
        logger.error(f"UNEXPECTED_ERROR: Failed to load birthdays: {e}")

    with _birthdays_cache_lock:
        _birthdays_cache = (mtime, data)
    return data


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
            _invalidate_birthdays_cache()
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

# is_user_celebrated_today + mark_* + get_announced_* call _load_announcements
# many times per celebration tick; each call took the cross-process FileLock.
# Cache by mtime to skip the lock on the read path; explicit invalidation on
# every write covers the case where two writes land within mtime resolution.
_announcements_cache_lock = threading.Lock()
_announcements_cache: tuple | None = None  # (mtime_or_None, dict)


def _invalidate_announcements_cache() -> None:
    global _announcements_cache
    with _announcements_cache_lock:
        _announcements_cache = None


def _default_announcements() -> dict:
    return {
        "birthdays": {},
        "timezone_birthdays": {},
        "special_days": {},
        "last_cleanup": None,
    }


def _load_announcements() -> dict:
    """
    Load announcements tracking data from JSON file (memoized by mtime).

    Returns:
        Dictionary with structure:
        {
            "birthdays": {"YYYY-MM-DD": ["user_id1", "user_id2"]},
            "timezone_birthdays": {"YYYY-MM-DD": {"user_id": "timezone"}},
            "special_days": {"YYYY-MM-DD": "ISO timestamp"},
            "last_cleanup": "ISO timestamp"
        }
    """
    global _announcements_cache

    try:
        mtime = os.path.getmtime(ANNOUNCEMENTS_FILE)
    except OSError:
        mtime = None

    with _announcements_cache_lock:
        if _announcements_cache is not None and _announcements_cache[0] == mtime:
            return _announcements_cache[1]

    data = _default_announcements()

    try:
        lock = FileLock(ANNOUNCEMENTS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
        with lock:
            if os.path.exists(ANNOUNCEMENTS_FILE):
                with open(ANNOUNCEMENTS_FILE, "r") as f:
                    data = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"FILE_ERROR: Failed to load announcements: {e}")

    with _announcements_cache_lock:
        _announcements_cache = (mtime, data)
    return data


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
        _invalidate_announcements_cache()
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

    for key in ("birthdays", "timezone_birthdays", "special_days"):
        data[key] = {k: v for k, v in data.get(key, {}).items() if k >= cutoff}

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


def try_mark_birthday_announced(user_id):
    """
    Atomically check if user was celebrated today and mark if not.

    This is a race-condition-safe version that holds the file lock throughout
    the entire check-and-mark operation.

    Args:
        user_id: User ID whose birthday to check and mark

    Returns:
        True if successfully marked (was not already celebrated)
        False if already celebrated today (no action taken)
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        lock = FileLock(ANNOUNCEMENTS_LOCK_FILE, timeout=TIMEOUTS["file_lock"])
        with lock:
            # Load within lock
            if os.path.exists(ANNOUNCEMENTS_FILE):
                with open(ANNOUNCEMENTS_FILE, "r") as f:
                    data = json.load(f)
            else:
                data = {"birthdays": {}, "timezone_birthdays": {}, "special_days": {}}

            # Check if already celebrated (within lock)
            if user_id in data.get("birthdays", {}).get(today, []):
                logger.debug(f"BIRTHDAY: {user_id} already celebrated today (atomic check)")
                return False

            # Also check timezone birthdays
            if user_id in data.get("timezone_birthdays", {}).get(today, {}):
                logger.debug(
                    f"BIRTHDAY: {user_id} already celebrated today via timezone (atomic check)"
                )
                return False

            # Mark as celebrated (within same lock)
            if "birthdays" not in data:
                data["birthdays"] = {}
            if today not in data["birthdays"]:
                data["birthdays"][today] = []

            data["birthdays"][today].append(user_id)

            # Save within lock
            with open(ANNOUNCEMENTS_FILE, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)

            _invalidate_announcements_cache()
            logger.info(f"BIRTHDAY: Atomically marked {user_id}'s birthday as announced")
            return True

    except Exception as e:
        logger.error(f"FILE_ERROR: Failed atomic check-and-mark for {user_id}: {e}")
        return False


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
    from utils.date_utils import get_timezone_object

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
