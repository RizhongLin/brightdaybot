"""
Tests for the @-mention tool agent: tool schemas, executors (privacy rules,
normalization, clamping), the bounded tool loop, and bot-only mention
stripping in the handler.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.mention_tools import (
    MENTION_TOOL_SCHEMAS,
    _normalize_user_id,
    execute_tool,
)

# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------


class TestSchemas:
    def test_flat_responses_api_shape(self):
        """Guard against accidental Chat Completions nesting ({"function": ...})."""
        for schema in MENTION_TOOL_SCHEMAS:
            assert schema["type"] == "function"
            assert "name" in schema  # flat, not nested under "function"
            assert "function" not in schema
            assert schema["parameters"]["type"] == "object"

    def test_expected_tools_present(self):
        names = {s["name"] for s in MENTION_TOOL_SCHEMAS}
        assert names == {"get_user_birthday", "get_upcoming_birthdays", "get_special_days"}


# -----------------------------------------------------------------------------
# Executors
# -----------------------------------------------------------------------------


class TestExecuteTool:
    def test_unknown_tool_returns_error_json(self):
        result = json.loads(execute_tool("nonexistent", "{}", MagicMock()))
        assert "error" in result

    def test_malformed_arguments_does_not_raise(self):
        result = json.loads(execute_tool("get_user_birthday", "{ not json", MagicMock()))
        assert result == {"error": "user_id is required"}

    def test_executor_exception_returns_error_json(self):
        with patch("storage.birthdays.get_birthday", side_effect=Exception("boom")):
            result = json.loads(execute_tool("get_user_birthday", '{"user_id": "U1"}', MagicMock()))
        assert "error" in result


class TestNormalizeUserId:
    def test_variants(self):
        assert _normalize_user_id("<@U123>") == "U123"
        assert _normalize_user_id("<@U123|bob>") == "U123"
        assert _normalize_user_id("u123") == "U123"
        assert _normalize_user_id("  U123  ") == "U123"
        assert _normalize_user_id(None) == ""


class TestGetUserBirthday:
    def _run(self, birthday_data, prefs=None, active=True):
        with (
            patch("storage.birthdays.get_birthday", return_value=birthday_data),
            patch("storage.birthdays.is_user_active", return_value=active),
            patch(
                "storage.birthdays.get_user_preferences",
                return_value=prefs or {"show_age": True},
            ),
            patch("services.mention_tools.get_username", return_value="alice"),
        ):
            return json.loads(
                execute_tool("get_user_birthday", '{"user_id": "<@U123>"}', MagicMock())
            )

    def test_found_with_age(self):
        result = self._run({"date": "25/12", "year": 1990})
        assert result["found"] is True
        assert result["username"] == "alice"
        assert result["date"] == "25/12"
        assert "age_turning" in result

    def test_show_age_false_hides_year(self):
        result = self._run({"date": "25/12", "year": 1990}, prefs={"show_age": False})
        assert result["found"] is True
        assert "year" not in result
        assert "age_turning" not in result

    def test_opted_out_reported_not_found(self):
        result = self._run({"date": "25/12"}, active=False)
        assert result == {"found": False}

    def test_missing_birthday_not_found(self):
        result = self._run(None)
        assert result == {"found": False}


class TestGetUpcomingBirthdays:
    def test_clamps_days_and_respects_show_age(self):
        groups = [
            {
                "date": "25/12",
                "date_words": "25th of December",
                "days_until": 3,
                "people": [
                    {"username": "alice", "year": 1990, "show_age": True},
                    {"username": "bob", "year": 1985, "show_age": False},
                ],
            },
            {
                "date": "01/01",
                "date_words": "1st of January",
                "days_until": 80,
                "people": [{"username": "carol", "year": None, "show_age": True}],
            },
        ]
        with (
            patch(
                "services.birthday_queries.get_upcoming_birthdays_grouped",
                return_value=groups,
            ),
            patch("storage.birthdays.load_birthdays", return_value={}),
        ):
            result = json.loads(
                execute_tool("get_upcoming_birthdays", '{"days_ahead": 7}', MagicMock())
            )

        assert result["days_ahead"] == 7
        names = [b["username"] for b in result["birthdays"]]
        assert names == ["alice", "bob"]  # carol filtered (80 > 7 days)
        alice = result["birthdays"][0]
        bob = result["birthdays"][1]
        assert "age_turning" in alice
        assert "age_turning" not in bob

    def test_invalid_days_ahead_uses_default(self):
        with (
            patch(
                "services.birthday_queries.get_upcoming_birthdays_grouped",
                return_value=[],
            ),
            patch("storage.birthdays.load_birthdays", return_value={}),
        ):
            result = json.loads(
                execute_tool("get_upcoming_birthdays", '{"days_ahead": "soon"}', MagicMock())
            )
        assert result["days_ahead"] == 7


class TestGetSpecialDays:
    def test_caps_days_per_date_and_truncates_description(self):
        day = SimpleNamespace(name="World Test Day", category="Culture", description="x" * 500)
        upcoming = {"10/06": [day, day, day, day, day]}
        with patch("storage.special_days.get_upcoming_special_days", return_value=upcoming):
            result = json.loads(
                execute_tool("get_special_days", '{"days_ahead": 200}', MagicMock())
            )

        assert result["days_ahead"] == 30  # clamped
        days = result["special_days"]["10/06"]
        assert len(days) == 3  # capped per date
        assert len(days[0]["description"]) == 150


# -----------------------------------------------------------------------------
# Tool loop
# -----------------------------------------------------------------------------


def _text_response(text):
    return SimpleNamespace(output_text=text, output=[])


def _call_response(call_id="c1", name="get_user_birthday", arguments='{"user_id": "U1"}'):
    call = SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments)
    reasoning = SimpleNamespace(type="reasoning")
    return SimpleNamespace(output_text="", output=[reasoning, call])


class TestToolLoop:
    def _generate(self, responses, execute_result='{"found": false}'):
        from services import mention_responder as mr

        calls = []

        def fake_complete_raw(**kwargs):
            calls.append(kwargs)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        with (
            patch.object(mr, "_build_tool_instructions", return_value="instructions"),
            patch("integrations.openai.complete_raw", side_effect=fake_complete_raw),
            patch("services.mention_tools.execute_tool", return_value=execute_result) as exec_tool,
        ):
            result = mr._generate_llm_response_with_tools("question", "U9", MagicMock())

        return result, calls, exec_tool

    def test_direct_answer_single_call(self):
        result, calls, exec_tool = self._generate([_text_response("Hello! 🎂")])
        assert result == "Hello! 🎂"
        assert len(calls) == 1
        exec_tool.assert_not_called()

    def test_tool_call_then_answer(self):
        result, calls, exec_tool = self._generate([_call_response(), _text_response("Found it!")])
        assert result == "Found it!"
        assert len(calls) == 2
        exec_tool.assert_called_once()

        # Second call's input must contain the prior output items AND the
        # function_call_output with the matching call_id
        second_input = calls[1]["input"]
        types = [getattr(i, "type", None) or i.get("type") for i in second_input[1:]]
        assert "reasoning" in types  # full output resent (gpt-5.x requirement)
        outputs = [
            i
            for i in second_input
            if isinstance(i, dict) and i.get("type") == "function_call_output"
        ]
        assert outputs and outputs[0]["call_id"] == "c1"

    def test_iterations_exhausted_forces_final_answer_without_tools(self):
        responses = [
            _call_response("c1"),
            _call_response("c2"),
            _call_response("c3"),
            _text_response("Final answer."),
        ]
        result, calls, _ = self._generate(responses)
        assert result == "Final answer."
        assert len(calls) == 4
        assert calls[3]["tools"] is None  # final forced call has no tools

    def test_empty_final_text_returns_none(self):
        result, _, _ = self._generate([_text_response("")])
        assert result is None


class TestGenerateMentionResponseFallback:
    def test_tool_exception_falls_back_to_legacy(self):
        from services import mention_responder as mr

        with (
            patch("config.MENTION_QA_TOOLS_ENABLED", True),
            patch.object(
                mr,
                "_generate_llm_response_with_tools",
                side_effect=Exception("boom"),
            ),
            patch.object(mr, "_build_context", return_value={}) as build_ctx,
            patch.object(mr, "_generate_llm_response", return_value="legacy") as legacy,
        ):
            result = mr.generate_mention_response(MagicMock(), "q", "general", "U1")

        assert result == "legacy"
        build_ctx.assert_called_once()
        legacy.assert_called_once()

    def test_tools_disabled_uses_legacy_only(self):
        from services import mention_responder as mr

        with (
            patch("config.MENTION_QA_TOOLS_ENABLED", False),
            patch.object(mr, "_generate_llm_response_with_tools") as tools_path,
            patch.object(mr, "_build_context", return_value={}),
            patch.object(mr, "_generate_llm_response", return_value="legacy"),
        ):
            result = mr.generate_mention_response(MagicMock(), "q", "general", "U1")

        assert result == "legacy"
        tools_path.assert_not_called()


# -----------------------------------------------------------------------------
# Bot-only mention stripping
# -----------------------------------------------------------------------------


class TestBotOnlyMentionStrip:
    def _handle(self, text, bot_user_id):
        from handlers import mention_handler as mh

        event = {"user": "U9", "text": text, "channel": "C1", "ts": "1.0"}
        captured = {}

        def fake_generate(app, question_text, question_type, user_id):
            captured["question"] = question_text
            return "ok"

        with (
            patch(
                "services.mention_responder.generate_mention_response",
                side_effect=fake_generate,
            ),
            patch.object(mh, "get_rate_limiter") as rl,
        ):
            rl.return_value.is_allowed.return_value = (True, 0)
            mh.handle_mention(MagicMock(), event, MagicMock(), bot_user_id=bot_user_id)

        return captured.get("question", "")

    def test_user_mention_preserved_when_bot_id_known(self):
        question = self._handle("<@UBOT> when is <@U456>'s birthday?", "UBOT")
        assert "<@U456>" in question
        assert "<@UBOT>" not in question

    def test_all_mentions_stripped_without_bot_id(self):
        question = self._handle("<@UBOT> when is <@U456>'s birthday?", None)
        assert "<@U456>" not in question
        assert "<@UBOT>" not in question
