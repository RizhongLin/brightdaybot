"""Tests for _build_api_params parameter shaping and complete() return contract."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from integrations.openai import _build_api_params, complete, complete_with_usage


def _build(model, **kwargs):
    return _build_api_params(
        messages=None,
        input_text="hello",
        instructions=None,
        model=model,
        max_tokens=kwargs.get("max_tokens"),
        temperature=kwargs.get("temperature"),
        reasoning_effort=kwargs.get("reasoning_effort"),
    )


class TestBuildApiParams:
    def test_gpt5_drops_temperature(self):
        """gpt-5* rejects temperature even when reasoning_effort isn't passed."""
        params = _build("gpt-5.5", temperature=0.7)
        assert "temperature" not in params

    def test_gpt5_with_reasoning_effort_keeps_reasoning_drops_temperature(self):
        params = _build("gpt-5.5", temperature=0.7, reasoning_effort="low")
        assert params.get("reasoning") == {"effort": "low"}
        assert "temperature" not in params

    def test_gpt4_keeps_temperature(self):
        """Non-reasoning models still get temperature."""
        params = _build("gpt-4.1", temperature=0.7)
        assert params["temperature"] == 0.7

    def test_o3_drops_temperature(self):
        params = _build("o3-mini", temperature=0.5)
        assert "temperature" not in params


def _fake_response(output_text, *, has_usage=True):
    usage = (
        SimpleNamespace(input_tokens=10, output_tokens=20, total_tokens=30) if has_usage else None
    )
    return SimpleNamespace(output_text=output_text, usage=usage)


class TestCompleteReturnContract:
    """complete() must never return None; reasoning models can yield empty text."""

    def test_complete_returns_empty_string_when_output_text_is_none(self):
        client = MagicMock()
        client.responses.create.return_value = _fake_response(None)
        with patch("integrations.openai.get_openai_client", return_value=client):
            result = complete(input_text="hi", model="gpt-5.5")
        assert result == ""

    def test_complete_returns_empty_string_when_output_text_is_empty(self):
        client = MagicMock()
        client.responses.create.return_value = _fake_response("")
        with patch("integrations.openai.get_openai_client", return_value=client):
            result = complete(input_text="hi", model="gpt-5.5")
        assert result == ""

    def test_complete_with_usage_returns_empty_string_not_none(self):
        client = MagicMock()
        client.responses.create.return_value = _fake_response(None)
        with patch("integrations.openai.get_openai_client", return_value=client):
            text, usage = complete_with_usage(input_text="hi", model="gpt-5.5")
        assert text == ""
        assert usage["output_tokens"] == 20  # usage still surfaces

    def test_complete_with_usage_propagates_api_errors(self):
        """No more silent (None, {}) — API failures must reach the caller."""
        client = MagicMock()
        client.responses.create.side_effect = RuntimeError("boom")
        with patch("integrations.openai.get_openai_client", return_value=client):
            with pytest.raises(RuntimeError):
                complete_with_usage(input_text="hi", model="gpt-5.5")


class TestToolsParam:
    def test_tools_included_when_provided(self):
        tools = [{"type": "function", "name": "f", "parameters": {}}]
        params = _build_api_params(
            messages=None,
            input_text="hi",
            instructions=None,
            model="gpt-5.5",
            max_tokens=None,
            temperature=None,
            reasoning_effort=None,
            tools=tools,
        )
        assert params["tools"] == tools

    def test_tools_absent_when_none(self):
        params = _build("gpt-5.5")
        assert "tools" not in params

    def test_list_input_passes_through_unchanged(self):
        items = [{"role": "user", "content": "hi"}, {"type": "function_call_output"}]
        params = _build_api_params(
            messages=None,
            input_text=items,
            instructions=None,
            model="gpt-5.5",
            max_tokens=None,
            temperature=None,
            reasoning_effort=None,
        )
        assert params["input"] is items


class TestCompleteRaw:
    def test_returns_raw_response_object(self):
        from integrations.openai import complete_raw

        client = MagicMock()
        fake = _fake_response("hello")
        client.responses.create.return_value = fake
        with patch("integrations.openai.get_openai_client", return_value=client):
            result = complete_raw(input="hi", model="gpt-5.5")
        assert result is fake

    def test_passes_tools_to_api(self):
        from integrations.openai import complete_raw

        client = MagicMock()
        client.responses.create.return_value = _fake_response("ok")
        tools = [{"type": "function", "name": "f", "parameters": {}}]
        with patch("integrations.openai.get_openai_client", return_value=client):
            complete_raw(input="hi", model="gpt-5.5", tools=tools)
        assert client.responses.create.call_args.kwargs["tools"] == tools
