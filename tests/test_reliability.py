"""
Tests for reliability hardening: birthdays.json corruption auto-restore,
startup environment validation, and the scheduler watchdog decision logic.
"""

import json
import os
from unittest.mock import patch

import pytest

# -----------------------------------------------------------------------------
# birthdays.json corruption auto-restore
# -----------------------------------------------------------------------------


class TestCorruptionAutoRestore:
    def _patched(self, b, tmp_path):
        path = tmp_path / "birthdays.json"
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        return (
            path,
            backup_dir,
            (
                patch.object(b, "BIRTHDAYS_JSON_FILE", str(path)),
                patch.object(b, "BIRTHDAYS_LOCK_FILE", str(path) + ".lock"),
                patch.object(b, "BACKUP_DIR", str(backup_dir)),
            ),
        )

    def test_corrupt_file_restored_from_backup(self, tmp_path):
        from storage import birthdays as b

        b._invalidate_birthdays_cache()
        path, backup_dir, patches = self._patched(b, tmp_path)

        good_data = {"U1": {"date": "01/01"}}
        (backup_dir / "birthdays_20260101_000000.json").write_text(json.dumps(good_data))
        path.write_text("{ this is not json")

        with patches[0], patches[1], patches[2]:
            result = b.load_birthdays()

        assert result == good_data
        # restored file replaces the corrupt one
        assert json.loads(path.read_text()) == good_data
        # corrupt original quarantined for forensics
        quarantined = [f for f in os.listdir(tmp_path) if ".corrupt-" in f]
        assert len(quarantined) == 1

    def test_corrupt_file_without_backup_returns_empty(self, tmp_path):
        from storage import birthdays as b

        b._invalidate_birthdays_cache()
        path, backup_dir, patches = self._patched(b, tmp_path)
        path.write_text("not json at all")

        with patches[0], patches[1], patches[2]:
            result = b.load_birthdays()

        assert result == {}

    def test_subsequent_load_uses_restored_data(self, tmp_path):
        from storage import birthdays as b

        b._invalidate_birthdays_cache()
        path, backup_dir, patches = self._patched(b, tmp_path)

        good_data = {"U2": {"date": "15/06"}}
        (backup_dir / "birthdays_20260101_000000.json").write_text(json.dumps(good_data))
        path.write_text("{ broken")

        with patches[0], patches[1], patches[2]:
            assert b.load_birthdays() == good_data
            # second call reads the now-healthy file (or cache) without error
            assert b.load_birthdays() == good_data


# -----------------------------------------------------------------------------
# Startup environment validation
# -----------------------------------------------------------------------------


class TestStartupEnvValidation:
    REQUIRED = [
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "OPENAI_API_KEY",
        "BIRTHDAY_CHANNEL_ID",
    ]

    def test_all_set_returns_empty(self):
        from utils.health import get_missing_required_env

        env = {var: "x" for var in self.REQUIRED}
        with patch.dict(os.environ, env, clear=False):
            assert get_missing_required_env() == []

    @pytest.mark.parametrize("missing_var", REQUIRED)
    def test_missing_var_reported(self, missing_var):
        from utils.health import get_missing_required_env

        env = {var: "x" for var in self.REQUIRED if var != missing_var}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop(missing_var, None)
            assert missing_var in get_missing_required_env()


# -----------------------------------------------------------------------------
# Scheduler watchdog decision logic
# -----------------------------------------------------------------------------


class TestWatchdogShouldExit:
    def test_grace_period_never_exits(self):
        from services.scheduler import watchdog_should_exit

        health = {"thread_alive": False, "heartbeat_age_seconds": 99999}
        assert watchdog_should_exit(health, uptime_seconds=10) is False

    def test_dead_thread_exits_after_grace(self):
        from services.scheduler import watchdog_should_exit

        health = {"thread_alive": False, "heartbeat_age_seconds": None}
        assert watchdog_should_exit(health, uptime_seconds=600) is True

    def test_healthy_scheduler_does_not_exit(self):
        from services.scheduler import watchdog_should_exit

        health = {"thread_alive": True, "heartbeat_age_seconds": 30}
        assert watchdog_should_exit(health, uptime_seconds=600) is False

    def test_stalled_heartbeat_exits(self):
        from config import HEARTBEAT_STALE_THRESHOLD_SECONDS
        from services.scheduler import watchdog_should_exit

        health = {
            "thread_alive": True,
            "heartbeat_age_seconds": 10 * HEARTBEAT_STALE_THRESHOLD_SECONDS + 1,
        }
        assert watchdog_should_exit(health, uptime_seconds=600) is True

    def test_mildly_stale_heartbeat_tolerated(self):
        from config import HEARTBEAT_STALE_THRESHOLD_SECONDS
        from services.scheduler import watchdog_should_exit

        health = {
            "thread_alive": True,
            "heartbeat_age_seconds": 2 * HEARTBEAT_STALE_THRESHOLD_SECONDS,
        }
        assert watchdog_should_exit(health, uptime_seconds=600) is False

    def test_no_heartbeat_recorded_with_alive_thread_tolerated(self):
        from services.scheduler import watchdog_should_exit

        health = {"thread_alive": True, "heartbeat_age_seconds": None}
        assert watchdog_should_exit(health, uptime_seconds=600) is False
