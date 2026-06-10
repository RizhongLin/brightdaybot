"""
Tests for DM birthday-setup deprecation modes, help-text consistency,
and immediate-celebration failure feedback.
"""

from unittest.mock import MagicMock, patch

# -----------------------------------------------------------------------------
# DM setup deprecation modes
# -----------------------------------------------------------------------------


class TestDmSetupModes:
    def test_disabled_rejects_with_redirect(self):
        from services import dispatcher as d

        say = MagicMock()
        with patch.object(d, "DM_BIRTHDAY_SETUP_MODE", "disabled"):
            assert d._dm_setup_rejected(say) is True
        say.assert_called_once()
        assert "/birthday" in say.call_args.kwargs.get("text", "")

    def test_nudge_and_enabled_do_not_reject(self):
        from services import dispatcher as d

        for mode in ("nudge", "enabled"):
            say = MagicMock()
            with patch.object(d, "DM_BIRTHDAY_SETUP_MODE", mode):
                assert d._dm_setup_rejected(say) is False
            say.assert_not_called()

    def test_nudge_sends_tip(self):
        from services import dispatcher as d

        say = MagicMock()
        with patch.object(d, "DM_BIRTHDAY_SETUP_MODE", "nudge"):
            d._send_dm_setup_nudge(say)
        say.assert_called_once()
        assert "/birthday" in say.call_args.args[0]

    def test_enabled_sends_no_tip(self):
        from services import dispatcher as d

        say = MagicMock()
        with patch.object(d, "DM_BIRTHDAY_SETUP_MODE", "enabled"):
            d._send_dm_setup_nudge(say)
        say.assert_not_called()

    def test_handle_dm_date_disabled_does_not_save(self):
        from services import dispatcher as d

        say = MagicMock()
        app = MagicMock()
        result = {"date": "25/12", "year": 1990, "status": "ok"}

        with (
            patch.object(d, "DM_BIRTHDAY_SETUP_MODE", "disabled"),
            patch.object(d, "save_birthday") as save,
        ):
            d.handle_dm_date(say, "U123", result, app)

        save.assert_not_called()
        say.assert_called_once()


# -----------------------------------------------------------------------------
# Help text consistency
# -----------------------------------------------------------------------------


def _all_text(blocks):
    text = []
    for block in blocks:
        if "text" in block and isinstance(block["text"], dict):
            text.append(block["text"].get("text", ""))
        for field in block.get("fields", []):
            text.append(field.get("text", ""))
        for el in block.get("elements", []):
            if isinstance(el, dict):
                text.append(el.get("text", ""))
    return "\n".join(text)


class TestHelpText:
    def test_user_help_documents_mention_qa(self):
        from slack.blocks import help as h

        with patch.object(h, "MENTION_QA_ENABLED", True):
            blocks, _ = h.build_help_blocks(is_admin=False)
        assert "@BrightDay" in _all_text(blocks)

    def test_user_help_hides_dm_shortcut_when_disabled(self):
        from slack.blocks import help as h

        with patch.object(h, "DM_BIRTHDAY_SETUP_MODE", "disabled"):
            blocks, _ = h.build_help_blocks(is_admin=False)
        text = _all_text(blocks)
        assert "add DD/MM" not in text
        assert "DM Shortcut" not in text

    def test_user_help_shows_dm_shortcut_by_default(self):
        from slack.blocks import help as h

        with patch.object(h, "DM_BIRTHDAY_SETUP_MODE", "nudge"):
            blocks, _ = h.build_help_blocks(is_admin=False)
        assert "DM Shortcut" in _all_text(blocks)

    def test_slash_help_mentions_qa_hint(self):
        from slack.blocks import help as h

        with patch.object(h, "MENTION_QA_ENABLED", True):
            blocks, _ = h.build_slash_help_blocks("birthday")
        assert "@BrightDay" in _all_text(blocks)


# -----------------------------------------------------------------------------
# Immediate celebration failure feedback
# -----------------------------------------------------------------------------


class TestImmediateCelebrationFailureFeedback:
    def test_modal_failure_sends_followup_dm(self):
        from handlers import modal_handler as m

        app = MagicMock()
        with (
            patch.object(m, "send_message") as send,
            patch(
                "commands.birthday_commands.send_immediate_birthday_announcement",
                side_effect=Exception("boom"),
            ),
        ):
            m._send_birthday_today_message(app, "U123", "alice", "10/06", 1990, False)

        # First call: the "Happy Birthday, saved!" message; last call: failure notice
        assert send.call_count >= 2
        last_text = send.call_args.args[2]
        assert "couldn't post the celebration" in last_text

    def test_modal_success_sends_no_failure_dm(self):
        from handlers import modal_handler as m

        app = MagicMock()
        with (
            patch.object(m, "send_message") as send,
            patch(
                "commands.birthday_commands.send_immediate_birthday_announcement",
            ),
        ):
            m._send_birthday_today_message(app, "U123", "alice", "10/06", 1990, False)

        for call in send.call_args_list:
            assert "couldn't post the celebration" not in str(call)

    def test_dm_date_failure_sends_followup(self):
        from services import dispatcher as d

        say = MagicMock()
        app = MagicMock()
        result = {"date": "10/06", "year": None, "status": "ok"}

        with (
            patch.object(d, "DM_BIRTHDAY_SETUP_MODE", "enabled"),
            patch.object(d, "save_birthday", return_value=False),
            patch.object(d, "get_username", return_value="alice"),
            patch.object(d, "check_if_birthday_today", return_value=True),
            patch.object(
                d,
                "send_immediate_birthday_announcement",
                side_effect=Exception("boom"),
            ),
            patch("storage.birthdays.trigger_external_backup"),
        ):
            d.handle_dm_date(say, "U123", result, app)

        texts = [str(c) for c in say.call_args_list]
        assert any("couldn't post the celebration" in t for t in texts)
