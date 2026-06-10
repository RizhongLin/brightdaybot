"""
System health monitoring for BrightDayBot.

Essential health checks: directories, files, API connectivity.
Main functions: get_system_status(), get_status_summary().
"""

import json
import os
from datetime import datetime

from config import (
    ADMINS_FILE,
    BACKUP_DIR,
    BIRTHDAY_CHANNEL,
    BIRTHDAYS_JSON_FILE,
    CACHE_DIR,
    DATA_DIR,
    DEFAULT_PERSONALITY,
    LOGS_DIR,
    PERSONALITY_FILE,
    SPECIAL_DAYS_ENABLED,
    SPECIAL_DAYS_JSON_FILE,
    STORAGE_DIR,
    TRACKING_DIR,
    get_logger,
)
from utils.log_setup import LOG_FILE_NAMES

logger = get_logger("system")

# Status codes
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_MISSING = "missing"
STATUS_NOT_CONFIGURED = "not_configured"


def format_timestamp(timestamp=None):
    """Format timestamps consistently."""
    if timestamp is None:
        dt = datetime.now()
    else:
        dt = datetime.fromtimestamp(timestamp)
    return dt.astimezone().isoformat()


def check_directory(directory_path):
    """Check if directory exists and is writable."""
    try:
        if not os.path.exists(directory_path):
            return {"status": STATUS_MISSING, "path": directory_path}
        if not os.path.isdir(directory_path):
            return {
                "status": STATUS_ERROR,
                "path": directory_path,
                "error": "Not a directory",
            }
        if not os.access(directory_path, os.R_OK | os.W_OK):
            return {
                "status": STATUS_ERROR,
                "path": directory_path,
                "error": "No read/write access",
            }
        return {"status": STATUS_OK, "path": directory_path}
    except Exception as e:
        return {"status": STATUS_ERROR, "path": directory_path, "error": str(e)}


def check_file(file_path):
    """Check if file exists and is readable."""
    try:
        if not os.path.exists(file_path):
            return {"status": STATUS_MISSING, "path": file_path}
        if not os.path.isfile(file_path):
            return {"status": STATUS_ERROR, "path": file_path, "error": "Not a file"}
        if not os.access(file_path, os.R_OK):
            return {"status": STATUS_ERROR, "path": file_path, "error": "Not readable"}

        # Add file size and modification time
        stat = os.stat(file_path)
        return {
            "status": STATUS_OK,
            "path": file_path,
            "size_bytes": stat.st_size,
            "last_modified": format_timestamp(stat.st_mtime),
        }
    except Exception as e:
        return {"status": STATUS_ERROR, "path": file_path, "error": str(e)}


def check_json_file(file_path):
    """Check if JSON file exists and is valid."""
    file_status = check_file(file_path)
    if file_status["status"] != STATUS_OK:
        return file_status

    try:
        with open(file_path, "r") as f:
            data = json.load(f)
        file_status["valid_json"] = True
        file_status["keys"] = list(data.keys()) if isinstance(data, dict) else None
        return file_status
    except json.JSONDecodeError as e:
        file_status["status"] = STATUS_ERROR
        file_status["error"] = f"Invalid JSON: {e}"
        return file_status
    except Exception as e:
        file_status["status"] = STATUS_ERROR
        file_status["error"] = str(e)
        return file_status


def check_environment():
    """Check required environment variables."""
    env_status = {"status": STATUS_OK, "variables": {}}

    required_vars = {
        "SLACK_BOT_TOKEN": "Slack bot authentication",
        "SLACK_APP_TOKEN": "Slack socket mode",
        "OPENAI_API_KEY": "OpenAI API access",
    }

    missing = []
    for var, _description in required_vars.items():
        value = os.environ.get(var)
        if value:
            # Mask the value for security
            env_status["variables"][var] = {"status": STATUS_OK, "set": True}
        else:
            env_status["variables"][var] = {"status": STATUS_MISSING, "set": False}
            missing.append(var)

    if missing:
        env_status["status"] = STATUS_ERROR
        env_status["missing"] = missing

    return env_status


def get_missing_required_env():
    """
    Return names of required environment variables that are not set.

    Includes BIRTHDAY_CHANNEL_ID on top of check_environment()'s secrets,
    since the bot cannot function without a target channel. Used for
    fail-fast validation at startup.

    Returns:
        list: Missing variable names (empty list when all are set)
    """
    env_status = check_environment()
    missing = list(env_status.get("missing", []))

    if not os.environ.get("BIRTHDAY_CHANNEL_ID"):
        missing.append("BIRTHDAY_CHANNEL_ID")

    return missing


def check_birthdays_file():
    """Check birthdays JSON file and count entries."""
    file_status = check_file(BIRTHDAYS_JSON_FILE)
    if file_status["status"] != STATUS_OK:
        return file_status

    try:
        with open(BIRTHDAYS_JSON_FILE, "r") as f:
            data = json.load(f)
            count = len(data)
        file_status["birthday_count"] = count
        return file_status
    except Exception as e:
        file_status["warning"] = f"Could not count birthdays: {e}"
        return file_status


def check_admin_config():
    """Check admin configuration file."""
    file_status = check_json_file(ADMINS_FILE)
    if file_status["status"] != STATUS_OK:
        return file_status

    try:
        with open(ADMINS_FILE, "r") as f:
            data = json.load(f)
        file_status["admin_count"] = len(data.get("admins", []))
        return file_status
    except Exception as e:
        file_status["warning"] = f"Could not count admins: {e}"
        return file_status


def check_personality_config():
    """Check personality configuration file."""
    file_status = check_json_file(PERSONALITY_FILE)
    if file_status["status"] != STATUS_OK:
        # Not critical - defaults will be used
        file_status["note"] = "Using default personality"
        return file_status

    try:
        with open(PERSONALITY_FILE, "r") as f:
            data = json.load(f)
        file_status["current_personality"] = data.get("current_personality", DEFAULT_PERSONALITY)
        file_status["has_custom_settings"] = "custom_settings" in data
        return file_status
    except Exception as e:
        file_status["warning"] = f"Could not read personality: {e}"
        return file_status


def check_special_days():
    """Check special days configuration."""
    if not SPECIAL_DAYS_ENABLED:
        return {"status": STATUS_OK, "enabled": False, "note": "Feature disabled"}

    file_status = check_file(SPECIAL_DAYS_JSON_FILE)
    if file_status["status"] != STATUS_OK:
        return file_status

    try:
        with open(SPECIAL_DAYS_JSON_FILE, "r") as f:
            data = json.load(f)
            count = len(data.get("days", []))
        file_status["enabled"] = True
        file_status["observance_count"] = count
        return file_status
    except Exception as e:
        file_status["warning"] = f"Could not count observances: {e}"
        return file_status


def check_log_files():
    """Check log files status."""
    if not os.path.exists(LOGS_DIR):
        return {"status": STATUS_MISSING, "path": LOGS_DIR}

    try:
        log_files = {}
        total_size = 0

        for log_name, log_filename in LOG_FILE_NAMES.items():
            log_path = os.path.join(LOGS_DIR, log_filename)
            if os.path.exists(log_path):
                size = os.path.getsize(log_path)
                total_size += size
                log_files[log_name] = {"exists": True, "size_kb": round(size / 1024, 1)}
            else:
                log_files[log_name] = {"exists": False}

        return {
            "status": STATUS_OK,
            "path": LOGS_DIR,
            "files": log_files,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }
    except Exception as e:
        return {"status": STATUS_ERROR, "path": LOGS_DIR, "error": str(e)}


def check_live_slack_connectivity(app=None):
    """Test live Slack API connectivity."""
    if app is None:
        return {"status": STATUS_NOT_CONFIGURED, "message": "No Slack app provided"}

    try:
        result = app.client.auth_test()
        if result.get("ok"):
            return {
                "status": STATUS_OK,
                "bot_user": result.get("user"),
                "team": result.get("team"),
                "bot_id": result.get("bot_id"),
            }
        else:
            return {
                "status": STATUS_ERROR,
                "error": result.get("error", "Unknown error"),
            }
    except Exception as e:
        return {"status": STATUS_ERROR, "error": str(e)}


def check_live_openai_connectivity():
    """Test live OpenAI API connectivity."""
    try:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return {
                "status": STATUS_NOT_CONFIGURED,
                "message": "OPENAI_API_KEY not set",
            }

        client = OpenAI(api_key=api_key)
        # Simple models list call to verify connectivity
        models = client.models.list()
        model_count = len(list(models))

        return {"status": STATUS_OK, "connected": True, "models_available": model_count}
    except Exception as e:
        return {"status": STATUS_ERROR, "error": str(e)}


def get_system_status(app=None, include_live_checks=False):
    """
    Get system health status.

    Args:
        app: Slack app instance for live connectivity checks
        include_live_checks: Whether to test actual API connectivity (slower)

    Returns:
        dict: Health status of all components
    """
    logger.debug("Running system health check")

    status = {
        "timestamp": format_timestamp(),
        "overall": STATUS_OK,
        "components": {},
    }

    has_error = False

    # Check directories
    dirs = {
        "data": DATA_DIR,
        "storage": STORAGE_DIR,
        "backup": BACKUP_DIR,
        "tracking": TRACKING_DIR,
        "cache": CACHE_DIR,
        "logs": LOGS_DIR,
    }

    dir_status = {}
    for name, path in dirs.items():
        result = check_directory(path)
        dir_status[name] = result
        if result["status"] == STATUS_ERROR:
            has_error = True
    status["components"]["directories"] = dir_status

    # Check environment variables
    env_status = check_environment()
    status["components"]["environment"] = env_status
    if env_status["status"] == STATUS_ERROR:
        has_error = True

    # Check critical files
    status["components"]["birthdays"] = check_birthdays_file()
    status["components"]["admins"] = check_admin_config()
    status["components"]["personality"] = check_personality_config()
    status["components"]["special_days"] = check_special_days()
    status["components"]["logs"] = check_log_files()

    # Check birthday channel config
    if BIRTHDAY_CHANNEL:
        status["components"]["birthday_channel"] = {
            "status": STATUS_OK,
            "channel": BIRTHDAY_CHANNEL,
        }
    else:
        status["components"]["birthday_channel"] = {"status": STATUS_NOT_CONFIGURED}
        has_error = True

    # Optional live connectivity checks
    if include_live_checks:
        status["components"]["slack_api"] = check_live_slack_connectivity(app)
        status["components"]["openai_api"] = check_live_openai_connectivity()

        if status["components"]["slack_api"]["status"] == STATUS_ERROR:
            has_error = True
        if status["components"]["openai_api"]["status"] == STATUS_ERROR:
            has_error = True

    status["overall"] = STATUS_ERROR if has_error else STATUS_OK

    if has_error:
        logger.warning(f"Health check complete: {status['overall']}")
    else:
        logger.debug(f"Health check complete: {status['overall']}")
    return status


def get_status_summary(app=None, include_live_checks=True):
    """Get human-readable status summary for admin command."""
    status = get_system_status(app=app, include_live_checks=include_live_checks)

    lines = [
        f"🤖 *BrightDayBot Health Check* ({status['timestamp']})",
        "",
    ]

    # Overall status
    if status["overall"] == STATUS_OK:
        lines.append("✅ *Overall Status*: Healthy")
    else:
        lines.append("❌ *Overall Status*: Issues detected")
    lines.append("")

    # Environment
    env = status["components"].get("environment", {})
    if env.get("status") == STATUS_OK:
        lines.append("✅ *Environment*: All required variables set")
    else:
        missing = env.get("missing", [])
        lines.append(f"❌ *Environment*: Missing: {', '.join(missing)}")

    # Birthdays
    birthdays = status["components"].get("birthdays", {})
    if birthdays.get("status") == STATUS_OK:
        count = birthdays.get("birthday_count", 0)
        lines.append(f"✅ *Birthdays*: {count} birthdays registered")
    elif birthdays.get("status") == STATUS_MISSING:
        lines.append("ℹ️ *Birthdays*: No birthdays file (will be created)")
    else:
        lines.append(f"❌ *Birthdays*: {birthdays.get('error', 'Error')}")

    # Admins
    admins = status["components"].get("admins", {})
    if admins.get("status") == STATUS_OK:
        count = admins.get("admin_count", 0)
        lines.append(f"✅ *Admins*: {count} admins configured")
    else:
        lines.append("ℹ️ *Admins*: Using default admin settings")

    # Personality
    personality = status["components"].get("personality", {})
    if personality.get("status") == STATUS_OK:
        current = personality.get("current_personality", DEFAULT_PERSONALITY)
        lines.append(f"✅ *Personality*: {current}")
    else:
        lines.append("ℹ️ *Personality*: Using default (standard)")

    # Special Days
    special = status["components"].get("special_days", {})
    if not special.get("enabled", True):
        lines.append("ℹ️ *Special Days*: Disabled")
    elif special.get("status") == STATUS_OK:
        count = special.get("observance_count", 0)
        lines.append(f"✅ *Special Days*: {count} observances configured")
    else:
        lines.append(f"❌ *Special Days*: {special.get('error', 'Error')}")

    # Birthday Channel
    channel = status["components"].get("birthday_channel", {})
    if channel.get("status") == STATUS_OK:
        lines.append(f"✅ *Birthday Channel*: {channel.get('channel')}")
    else:
        lines.append("❌ *Birthday Channel*: Not configured")

    # Logs
    logs = status["components"].get("logs", {})
    if logs.get("status") == STATUS_OK:
        size = logs.get("total_size_mb", 0)
        lines.append(f"✅ *Logs*: {size} MB total")
    else:
        lines.append(f"ℹ️ *Logs*: {logs.get('status', 'Unknown')}")

    # Live API checks
    if include_live_checks:
        lines.append("")
        lines.append("*API Connectivity:*")

        slack = status["components"].get("slack_api", {})
        if slack.get("status") == STATUS_OK:
            lines.append(f"✅ *Slack*: Connected as {slack.get('bot_user', 'unknown')}")
        elif slack.get("status") == STATUS_NOT_CONFIGURED:
            lines.append("ℹ️ *Slack*: Not tested (no app provided)")
        else:
            lines.append(f"❌ *Slack*: {slack.get('error', 'Error')}")

        openai = status["components"].get("openai_api", {})
        if openai.get("status") == STATUS_OK:
            lines.append("✅ *OpenAI*: Connected")
        elif openai.get("status") == STATUS_NOT_CONFIGURED:
            lines.append("❌ *OpenAI*: API key not configured")
        else:
            lines.append(f"❌ *OpenAI*: {openai.get('error', 'Error')}")

    return "\n".join(lines)
