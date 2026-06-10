"""
Tests for daily AI usage accounting (storage/ai_usage.py) and the canvas
AI usage section.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class TestRecordUsage:
    def _patched(self, mod, tmp_path):
        return (
            patch.object(mod, "AI_USAGE_FILE", str(tmp_path / "ai_usage_daily.json")),
            patch.object(mod, "TRACKING_DIR", str(tmp_path)),
        )

    def test_record_and_aggregate_same_day(self, tmp_path):
        from storage import ai_usage as au

        p1, p2 = self._patched(au, tmp_path)
        with p1, p2:
            au.record_usage("BIRTHDAY", 100, 50)
            au.record_usage("BIRTHDAY", 200, 100)
            au.record_usage("MENTION", 10, 5)
            today = au.get_today_usage()

        assert today["calls"] == 3
        assert today["input_tokens"] == 310
        assert today["output_tokens"] == 155
        assert today["by_context"]["BIRTHDAY"]["calls"] == 2
        assert today["by_context"]["MENTION"]["input_tokens"] == 10

    def test_old_days_pruned_on_write(self, tmp_path):
        from storage import ai_usage as au

        path = tmp_path / "ai_usage_daily.json"
        old_day = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        path.write_text(
            json.dumps(
                {old_day: {"calls": 5, "input_tokens": 1, "output_tokens": 1, "by_context": {}}}
            )
        )

        p1, p2 = self._patched(au, tmp_path)
        with p1, p2:
            au.record_usage("X", 1, 1)

        data = json.loads(path.read_text())
        assert old_day not in data
        assert len(data) == 1

    def test_corrupt_file_resets_without_raising(self, tmp_path):
        from storage import ai_usage as au

        path = tmp_path / "ai_usage_daily.json"
        path.write_text("{ not json")

        p1, p2 = self._patched(au, tmp_path)
        with p1, p2:
            au.record_usage("X", 1, 2)
            today = au.get_today_usage()

        assert today["calls"] == 1

    def test_get_today_usage_empty_when_no_file(self, tmp_path):
        from storage import ai_usage as au

        p1, p2 = self._patched(au, tmp_path)
        with p1, p2:
            today = au.get_today_usage()

        assert today == {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "by_context": {},
        }

    def test_record_never_raises_on_bad_input(self, tmp_path):
        from storage import ai_usage as au

        p1, p2 = self._patched(au, tmp_path)
        with p1, p2:
            au.record_usage(None, None, None)  # must not raise
            today = au.get_today_usage()

        assert today["calls"] == 1
        assert today["input_tokens"] == 0


class TestCanvasAiUsageSection:
    def test_section_with_usage(self):
        from slack import canvas

        usage = {
            "calls": 7,
            "input_tokens": 1200,
            "output_tokens": 800,
            "by_context": {
                "BIRTHDAY": {"calls": 5, "input_tokens": 1000, "output_tokens": 700},
                "MENTION": {"calls": 2, "input_tokens": 200, "output_tokens": 100},
            },
        }
        with patch("storage.ai_usage.get_today_usage", return_value=usage):
            section = canvas._build_ai_usage_section()

        assert "7" in section
        assert "BIRTHDAY" in section
        assert "1,200" in section

    def test_section_with_no_usage(self):
        from slack import canvas

        empty = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_context": {}}
        with patch("storage.ai_usage.get_today_usage", return_value=empty):
            section = canvas._build_ai_usage_section()

        assert "No AI calls yet today" in section
