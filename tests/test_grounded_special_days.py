"""
Tests for grounded special-day announcements: source-priority ordering,
per-day cap, grounded intro prompts, description quoting in blocks,
lazy description enrichment, and the birthday thread engagement prompt.
"""

import json
from unittest.mock import MagicMock, patch

from storage.special_days import SpecialDay, _source_priority


def _day(name="World Test Day", source="UN", description="", url="", date="10/06"):
    return SpecialDay(
        date=date,
        name=name,
        category="Culture",
        description=description,
        emoji="🌍",
        source=source,
        url=url,
    )


# -----------------------------------------------------------------------------
# Source priority + cap
# -----------------------------------------------------------------------------


class TestSourcePriority:
    def test_official_sources_rank_first(self):
        assert _source_priority(_day(source="UN")) == 0
        assert _source_priority(_day(source="WHO")) == 0
        assert _source_priority(_day(source="UNESCO")) == 0
        assert _source_priority(_day(source="Calendarific (US)")) == 1
        assert _source_priority(_day(source="ICS: Team Calendar")) == 1
        assert _source_priority(_day(source="")) == 2
        assert _source_priority(_day(source="Custom")) == 2

    @staticmethod
    def _isolated_sources(sd, days):
        """Patch out live observance sources so only `days` flow through."""
        return (
            patch.object(sd, "UN_OBSERVANCES_ENABLED", False),
            patch.object(sd, "UNESCO_OBSERVANCES_ENABLED", False),
            patch.object(sd, "WHO_OBSERVANCES_ENABLED", False),
            patch.object(sd, "CALENDARIFIC_ENABLED", False),
            patch.object(sd, "ICS_SUBSCRIPTIONS_ENABLED", False),
            patch.object(sd, "load_special_days", return_value=days),
        )

    def test_get_special_days_sorted_and_capped(self):
        from datetime import datetime

        from storage import special_days as sd

        days = [
            _day(name="Custom Day", source=""),
            _day(name="Z UN Day", source="UN"),
            _day(name="A Calendarific Day", source="Calendarific (US)"),
            _day(name="A UN Day", source="UN"),
        ]

        p = self._isolated_sources(sd, days)
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            with patch.object(sd, "MAX_SPECIAL_DAYS_PER_DAY", 2):
                result = sd.get_special_days_for_date(datetime(2026, 6, 10))

        names = [d.name for d in result]
        assert names == ["A UN Day", "Z UN Day"]  # official first, capped at 2

    def test_no_cap_when_zero(self):
        from datetime import datetime

        from storage import special_days as sd

        days = [
            _day(name="Custom Day", source=""),
            _day(name="UN Day", source="UN"),
        ]

        p = self._isolated_sources(sd, days)
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            with patch.object(sd, "MAX_SPECIAL_DAYS_PER_DAY", 0):
                result = sd.get_special_days_for_date(datetime(2026, 6, 10))

        assert [d.name for d in result] == ["UN Day", "Custom Day"]


# -----------------------------------------------------------------------------
# Grounded intro prompt selection
# -----------------------------------------------------------------------------


class TestGroundedPrompt:
    def _generate(self, day, grounded_enabled=True):
        from services import special_day as svc

        captured = {}

        def fake_complete(**kwargs):
            captured["messages"] = kwargs.get("messages")
            return "A themed intro!"

        with (
            patch.object(svc, "GROUNDED_SPECIAL_DAYS_ENABLED", grounded_enabled),
            patch.object(svc, "complete", side_effect=fake_complete),
            patch.object(svc, "get_emoji_context_for_ai", return_value={"emoji_examples": "🎉"}),
        ):
            result = svc.generate_special_day_message([day], include_facts=False, use_teaser=True)

        prompt = captured["messages"][1]["content"]
        return result, prompt

    def test_described_day_uses_grounded_intro_prompt(self):
        day = _day(description="Official UN description of the day.")
        result, prompt = self._generate(day)
        assert "Do NOT state facts" in prompt
        assert result == "A themed intro!"

    def test_day_without_description_uses_classic_prompt(self):
        day = _day(description="")
        _, prompt = self._generate(day)
        assert "Do NOT state facts" not in prompt

    def test_flag_off_uses_classic_prompt(self):
        day = _day(description="Official description.")
        _, prompt = self._generate(day, grounded_enabled=False)
        assert "Do NOT state facts" not in prompt


# -----------------------------------------------------------------------------
# Description quote in blocks
# -----------------------------------------------------------------------------


class TestDescriptionQuoteBlocks:
    def test_quote_block_present_for_described_day(self):
        from slack.blocks import special_day as blocks_mod

        day = _day(description="The UN designates this day to remind everyone.")
        with patch.object(blocks_mod, "GROUNDED_SPECIAL_DAYS_ENABLED", True):
            blocks, _ = blocks_mod.build_special_day_blocks([day], "Intro!")

        texts = [b.get("text", {}).get("text", "") for b in blocks if "text" in b]
        assert any(t.startswith("> The UN designates") for t in texts)

    def test_quote_truncated(self):
        from slack.blocks import special_day as blocks_mod

        day = _day(description="x" * 1000)
        with (
            patch.object(blocks_mod, "GROUNDED_SPECIAL_DAYS_ENABLED", True),
            patch.object(blocks_mod, "SPECIAL_DAY_QUOTE_MAX_CHARS", 100),
        ):
            blocks, _ = blocks_mod.build_special_day_blocks([day], "Intro!")

        quote = next(
            b["text"]["text"]
            for b in blocks
            if "text" in b and b["text"].get("text", "").startswith("> ")
        )
        assert len(quote) <= 105  # "> " + 100 chars + ellipsis

    def test_no_quote_when_flag_off(self):
        from slack.blocks import special_day as blocks_mod

        day = _day(description="Official description.")
        with patch.object(blocks_mod, "GROUNDED_SPECIAL_DAYS_ENABLED", False):
            blocks, _ = blocks_mod.build_special_day_blocks([day], "Intro!")

        texts = [b.get("text", {}).get("text", "") for b in blocks if "text" in b]
        assert not any(t.startswith("> ") for t in texts)


# -----------------------------------------------------------------------------
# Lazy description enrichment
# -----------------------------------------------------------------------------


class TestDescriptionEnrichment:
    def _reset(self, mod, tmp_path):
        mod._cache_state = None
        return str(tmp_path / "observance_descriptions.json")

    def test_cache_hit_returned_without_fetch(self, tmp_path):
        from integrations import observance_descriptions as od

        cache_file = self._reset(od, tmp_path)
        with open(cache_file, "w") as f:
            json.dump({"https://un.org/x": {"name": "X", "description": "Cached."}}, f)

        with (
            patch.object(od, "OBSERVANCE_DESCRIPTIONS_CACHE_FILE", cache_file),
            patch.object(od, "_fetch_page_text") as fetch,
        ):
            assert od.get_official_description("X", "https://un.org/x") == "Cached."
            fetch.assert_not_called()

    def test_miss_fetches_extracts_and_caches(self, tmp_path):
        from integrations import observance_descriptions as od

        cache_file = self._reset(od, tmp_path)

        with (
            patch.object(od, "OBSERVANCE_DESCRIPTIONS_CACHE_FILE", cache_file),
            patch.object(od, "_fetch_page_text", return_value="Official page text."),
            patch(
                "integrations.openai.complete",
                return_value="A faithful description.",
            ),
        ):
            result = od.get_official_description("X Day", "https://un.org/x")

        assert result == "A faithful description."
        cached = json.load(open(cache_file))
        assert cached["https://un.org/x"]["description"] == "A faithful description."

    def test_fetch_failure_returns_empty_and_not_cached(self, tmp_path):
        from integrations import observance_descriptions as od

        cache_file = self._reset(od, tmp_path)

        with (
            patch.object(od, "OBSERVANCE_DESCRIPTIONS_CACHE_FILE", cache_file),
            patch.object(od, "_fetch_page_text", side_effect=Exception("timeout")),
        ):
            assert od.get_official_description("X", "https://un.org/x") == ""

        import os

        assert not os.path.exists(cache_file)

    def test_apply_cached_descriptions_fills_in_place(self, tmp_path):
        from integrations import observance_descriptions as od

        cache_file = self._reset(od, tmp_path)
        with open(cache_file, "w") as f:
            json.dump({"https://un.org/x": {"name": "X", "description": "Cached."}}, f)

        day = _day(description="", url="https://un.org/x")
        other = _day(name="No URL Day", description="", url="")

        with patch.object(od, "OBSERVANCE_DESCRIPTIONS_CACHE_FILE", cache_file):
            od.apply_cached_descriptions([day, other])

        assert day.description == "Cached."
        assert other.description == ""

    def test_non_https_url_skipped(self, tmp_path):
        from integrations import observance_descriptions as od

        cache_file = self._reset(od, tmp_path)
        with patch.object(od, "OBSERVANCE_DESCRIPTIONS_CACHE_FILE", cache_file):
            assert od._fetch_page_text("http://insecure.example.com") == ""


# -----------------------------------------------------------------------------
# Birthday thread engagement prompt
# -----------------------------------------------------------------------------


class TestEngagementPrompt:
    def _pipeline(self):
        from services.celebration import BirthdayCelebrationPipeline

        return BirthdayCelebrationPipeline(MagicMock(), "C123", mode="daily")

    def _person(self, style="standard"):
        return {
            "user_id": "U1",
            "username": "alice",
            "preferences": {"celebration_style": style},
        }

    def test_prompt_posted_for_standard_style(self):
        pipeline = self._pipeline()
        with (
            patch("config.BIRTHDAY_THREAD_PROMPT_ENABLED", True),
            patch("services.celebration.send_message") as send,
        ):
            pipeline._add_engagement_prompt("123.456", [self._person("standard")])

        send.assert_called_once()
        assert send.call_args.kwargs.get("thread_ts") == "123.456"
        assert "<@U1>" in send.call_args.args[2]

    def test_prompt_skipped_for_quiet_style(self):
        pipeline = self._pipeline()
        with (
            patch("config.BIRTHDAY_THREAD_PROMPT_ENABLED", True),
            patch("services.celebration.send_message") as send,
        ):
            pipeline._add_engagement_prompt("123.456", [self._person("quiet")])

        send.assert_not_called()

    def test_prompt_skipped_when_flag_off(self):
        pipeline = self._pipeline()
        with (
            patch("config.BIRTHDAY_THREAD_PROMPT_ENABLED", False),
            patch("services.celebration.send_message") as send,
        ):
            pipeline._add_engagement_prompt("123.456", [self._person("standard")])

        send.assert_not_called()
