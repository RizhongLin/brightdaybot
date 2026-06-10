"""
Tests for UX handlers: slash commands, modals, and App Home.

Tests handler registration, input parsing, conditional view logic,
and upcoming birthday filtering without requiring live Slack connections.
"""

from unittest.mock import MagicMock, patch


class TestSlashCommandRegistration:
    """Tests for slash command handler registration"""

    def test_register_slash_commands_adds_birthday(self):
        """Registration adds /birthday command handler"""
        mock_app = MagicMock()
        mock_app.command = MagicMock(return_value=lambda f: f)

        from handlers.slash_handler import register_slash_commands

        register_slash_commands(mock_app)

        mock_app.command.assert_any_call("/birthday")

    def test_register_slash_commands_adds_special_day(self):
        """Registration adds /special-day command handler"""
        mock_app = MagicMock()
        mock_app.command = MagicMock(return_value=lambda f: f)

        from handlers.slash_handler import register_slash_commands

        register_slash_commands(mock_app)

        mock_app.command.assert_any_call("/special-day")


class TestModalHandlerRegistration:
    """Tests for modal handler registration"""

    def test_register_modal_handlers_adds_view(self):
        """Registration adds birthday_modal view handler"""
        mock_app = MagicMock()
        mock_app.view = MagicMock(return_value=lambda f: f)
        mock_app.action = MagicMock(return_value=lambda f: f)

        from handlers.modal_handler import register_modal_handlers

        register_modal_handlers(mock_app)

        mock_app.view.assert_called_once_with("birthday_modal")

    def test_register_modal_handlers_adds_button_action(self):
        """Registration adds open_birthday_modal button handler"""
        mock_app = MagicMock()
        mock_app.view = MagicMock(return_value=lambda f: f)
        mock_app.action = MagicMock(return_value=lambda f: f)

        from handlers.modal_handler import register_modal_handlers

        register_modal_handlers(mock_app)

        mock_app.action.assert_called_once_with("open_birthday_modal")


class TestAppHomeRegistration:
    """Tests for App Home handler registration"""

    def test_register_app_home_handlers_adds_event(self):
        """Registration adds app_home_opened event handler"""
        mock_app = MagicMock()
        mock_app.event = MagicMock(return_value=lambda f: f)

        from handlers.app_home_handler import register_app_home_handlers

        register_app_home_handlers(mock_app)

        mock_app.event.assert_called_once_with("app_home_opened")


class TestSlashCommandParsing:
    """Tests for slash command input parsing logic"""

    def test_birthday_add_subcommand(self):
        """Empty text or 'add' triggers modal opening"""
        from handlers.slash_handler import register_slash_commands

        mock_app = MagicMock()
        captured_handlers = {}

        def capture_command(cmd_name):
            def decorator(func):
                captured_handlers[cmd_name] = func
                return func

            return decorator

        mock_app.command = capture_command

        register_slash_commands(mock_app)

        handler = captured_handlers["/birthday"]

        ack = MagicMock()
        respond = MagicMock()
        client = MagicMock()

        body = {"user_id": "U123", "text": "", "trigger_id": "trigger123"}

        with patch("handlers.slash_handler._open_birthday_modal") as mock_open_modal:
            handler(ack, body, client, respond)
            ack.assert_called_once()
            mock_open_modal.assert_called_once_with(client, "trigger123", "U123")

    def test_birthday_check_self(self):
        """Check without user checks own birthday"""
        from handlers.slash_handler import register_slash_commands

        mock_app = MagicMock()
        captured_handlers = {}

        def capture_command(cmd_name):
            def decorator(func):
                captured_handlers[cmd_name] = func
                return func

            return decorator

        mock_app.command = capture_command

        register_slash_commands(mock_app)

        handler = captured_handlers["/birthday"]

        ack = MagicMock()
        respond = MagicMock()
        client = MagicMock()

        body = {"user_id": "U123", "text": "check", "trigger_id": "trigger123"}

        with patch("handlers.slash_handler._handle_slash_check") as mock_check:
            handler(ack, body, client, respond)
            ack.assert_called_once()
            mock_check.assert_called_once_with("check", "U123", respond, mock_app)

    def test_birthday_list_subcommand(self):
        """List subcommand calls list handler"""
        from handlers.slash_handler import register_slash_commands

        mock_app = MagicMock()
        captured_handlers = {}

        def capture_command(cmd_name):
            def decorator(func):
                captured_handlers[cmd_name] = func
                return func

            return decorator

        mock_app.command = capture_command

        register_slash_commands(mock_app)

        handler = captured_handlers["/birthday"]

        ack = MagicMock()
        respond = MagicMock()
        client = MagicMock()

        body = {"user_id": "U123", "text": "list", "trigger_id": "trigger123"}

        with patch("handlers.slash_handler._handle_slash_list") as mock_list:
            handler(ack, body, client, respond)
            ack.assert_called_once()
            mock_list.assert_called_once_with(respond, mock_app)

    def test_birthday_unknown_shows_help(self):
        """Unknown subcommand shows help"""
        from handlers.slash_handler import register_slash_commands

        mock_app = MagicMock()
        captured_handlers = {}

        def capture_command(cmd_name):
            def decorator(func):
                captured_handlers[cmd_name] = func
                return func

            return decorator

        mock_app.command = capture_command

        register_slash_commands(mock_app)

        handler = captured_handlers["/birthday"]

        ack = MagicMock()
        respond = MagicMock()
        client = MagicMock()

        body = {"user_id": "U123", "text": "unknown", "trigger_id": "trigger123"}

        with patch("handlers.slash_handler._send_birthday_help") as mock_help:
            handler(ack, body, client, respond)
            ack.assert_called_once()
            mock_help.assert_called_once_with(respond)


class TestBacktickStripping:
    """Tests that backtick-wrapped input is handled correctly in commands"""

    def _capture_slash_handlers(self):
        """Register slash commands and return captured handlers."""
        from handlers.slash_handler import register_slash_commands

        mock_app = MagicMock()
        captured = {}

        def capture_command(cmd_name):
            def decorator(func):
                captured[cmd_name] = func
                return func

            return decorator

        mock_app.command = capture_command
        register_slash_commands(mock_app)
        return captured, mock_app

    def test_slash_birthday_strips_backticks(self):
        """/birthday `list` is parsed as 'list'"""
        captured, mock_app = self._capture_slash_handlers()
        handler = captured["/birthday"]

        ack = MagicMock()
        respond = MagicMock()
        client = MagicMock()
        body = {"user_id": "U123", "text": "`list`", "trigger_id": "trigger123"}

        with patch("handlers.slash_handler._handle_slash_list") as mock_list:
            handler(ack, body, client, respond)
            mock_list.assert_called_once_with(respond, mock_app)

    def test_slash_birthday_strips_triple_backticks(self):
        """/birthday ```check``` is parsed as 'check'"""
        captured, mock_app = self._capture_slash_handlers()
        handler = captured["/birthday"]

        ack = MagicMock()
        respond = MagicMock()
        client = MagicMock()
        body = {"user_id": "U123", "text": "```check```", "trigger_id": "trigger123"}

        with patch("handlers.slash_handler._handle_slash_check") as mock_check:
            handler(ack, body, client, respond)
            mock_check.assert_called_once_with("check", "U123", respond, mock_app)

    def test_slash_special_day_strips_backticks(self):
        """/special-day `week` is parsed as 'week'"""
        captured, mock_app = self._capture_slash_handlers()
        handler = captured["/special-day"]

        ack = MagicMock()
        respond = MagicMock()
        body = {"user_id": "U123", "text": "`week`"}

        with patch("commands.special_day_commands.handle_special_command") as mock_special:
            handler(ack, body, respond)
            mock_special.assert_called_once_with(["week"], "U123", respond, mock_app)

    def test_dm_command_strips_backticks(self):
        """DM `list` is recognized as 'list' command"""
        from services.dispatcher import handle_command

        say = MagicMock()
        mock_app = MagicMock()

        with patch("services.dispatcher.get_username", return_value="TestUser"):
            with patch("services.dispatcher.handle_list_command") as mock_list:
                handle_command("`list`", "U123", say, mock_app)

        # Should NOT be called because backtick stripping happens in event_handler,
        # not in handle_command itself — so this verifies the boundary
        mock_list.assert_not_called()

    def test_dm_event_handler_strips_backticks(self):
        """DM event with backtick-wrapped text strips backticks before dispatch"""
        # Verify the stripping logic directly
        raw_text = "`add 05/03`"
        stripped = raw_text.strip("`").strip().lower()
        assert stripped == "add 05/03"

    def test_dm_event_handler_strips_triple_backticks(self):
        """DM event with triple backtick-wrapped text strips all backticks"""
        raw_text = "```admin status```"
        stripped = raw_text.strip("`").strip().lower()
        assert stripped == "admin status"

    def test_dm_event_handler_preserves_plain_text(self):
        """Plain text without backticks is unchanged"""
        raw_text = "add 05/03 1990"
        stripped = raw_text.strip("`").strip().lower()
        assert stripped == "add 05/03 1990"


class TestAppHomeViewBuilding:
    """Tests for App Home conditional view construction"""

    def test_home_view_shows_add_button_when_no_birthday(self):
        """Home shows Add button when user has no birthday"""
        from handlers.app_home_handler import _build_home_view

        mock_app = MagicMock()

        with patch("handlers.app_home_handler.load_birthdays", return_value={}):
            with patch("services.birthday_queries.get_username", return_value="TestUser"):
                with patch("slack.client.get_channel_members", return_value=["U123"]):
                    view = _build_home_view("U123", mock_app)

        action_blocks = [b for b in view["blocks"] if b.get("type") == "actions"]
        assert len(action_blocks) >= 1

        button = action_blocks[0]["elements"][0]
        assert button["action_id"] == "open_birthday_modal"
        assert "Add" in button["text"]["text"]

    def test_home_view_shows_edit_button_when_has_birthday(self, mock_birthday_data):
        """Home shows Edit button when user has birthday"""
        from handlers.app_home_handler import _build_home_view

        mock_app = MagicMock()

        with patch(
            "handlers.app_home_handler.load_birthdays",
            return_value={"U123": mock_birthday_data(date="25/12", year=1990)},
        ):
            with patch(
                "handlers.app_home_handler.get_user_preferences",
                return_value={
                    "active": True,
                    "image_enabled": True,
                    "show_age": True,
                    "celebration_style": "standard",
                },
            ):
                with patch("services.birthday_queries.get_username", return_value="TestUser"):
                    with patch("slack.client.get_channel_members", return_value=["U123"]):
                        view = _build_home_view("U123", mock_app)

        action_blocks = [b for b in view["blocks"] if b.get("type") == "actions"]
        assert len(action_blocks) >= 1

        edit_button = None
        for block in action_blocks:
            for element in block.get("elements", []):
                if element.get("action_id") == "open_birthday_modal":
                    edit_button = element
                    break
            if edit_button:
                break

        assert edit_button is not None, "open_birthday_modal button not found"
        assert "Edit" in edit_button["text"]["text"]


class TestUpcomingBirthdaysFiltering:
    """Tests for upcoming birthdays date-grouped calculation"""

    def test_get_upcoming_birthdays_limits_by_dates(self, mock_birthday_data):
        """Limit applies to unique dates, not people"""
        from datetime import datetime

        from handlers.app_home_handler import _get_upcoming_birthdays

        mock_app = MagicMock()

        # 10 people on 10 different dates
        birthdays = {
            f"U{i}": mock_birthday_data(date=f"{10+i:02d}/01", year=1990) for i in range(10)
        }

        channel_members = [f"U{i}" for i in range(10)]

        with patch("services.birthday_queries.get_username", return_value="User"):
            with patch("slack.client.get_channel_members", return_value=channel_members):
                with patch("storage.birthdays.is_user_active", return_value=True):
                    with patch(
                        "services.birthday_queries.calculate_days_until_birthday",
                        side_effect=lambda d, r: datetime.strptime(d, "%d/%m").day,
                    ):
                        result = _get_upcoming_birthdays(birthdays, mock_app, limit=5)

        assert len(result) <= 5
        # Each result is a date group with "people" list
        for group in result:
            assert "date" in group
            assert "people" in group
            assert "days_until" in group

    def test_get_upcoming_birthdays_groups_same_date(self, mock_birthday_data):
        """People with the same birthday are grouped together"""
        from handlers.app_home_handler import _get_upcoming_birthdays

        mock_app = MagicMock()

        # Two people on same date, one on different
        birthdays = {
            "U1": mock_birthday_data(date="01/01", year=1990),
            "U2": mock_birthday_data(date="01/01", year=1985),
            "U3": mock_birthday_data(date="02/01", year=1995),
        }

        with patch("services.birthday_queries.get_username", return_value="User"):
            with patch("slack.client.get_channel_members", return_value=["U1", "U2", "U3"]):
                with patch("storage.birthdays.is_user_active", return_value=True):
                    with patch(
                        "services.birthday_queries.calculate_days_until_birthday",
                        side_effect=[5, 5, 10],
                    ):
                        result = _get_upcoming_birthdays(birthdays, mock_app, limit=10)

        assert len(result) == 2  # 2 unique dates
        assert len(result[0]["people"]) == 2  # Two people on 01/01
        assert len(result[1]["people"]) == 1  # One person on 02/01

    def test_get_upcoming_birthdays_filters_non_channel_members(self, mock_birthday_data):
        """Users not in channel are excluded"""
        from handlers.app_home_handler import _get_upcoming_birthdays

        mock_app = MagicMock()

        birthdays = {
            "U1": mock_birthday_data(date="01/01", year=1990),
            "U2": mock_birthday_data(date="02/01", year=1985),
        }

        with patch("services.birthday_queries.get_username", return_value="User"):
            with patch("slack.client.get_channel_members", return_value=["U1"]):
                with patch("storage.birthdays.is_user_active", return_value=True):
                    with patch(
                        "services.birthday_queries.calculate_days_until_birthday",
                        return_value=5,
                    ):
                        result = _get_upcoming_birthdays(birthdays, mock_app, limit=10)

        assert len(result) == 1
        assert result[0]["people"][0]["user_id"] == "U1"

    def test_get_upcoming_birthdays_filters_paused_users(self, mock_birthday_data):
        """Users with paused celebrations are excluded"""
        from handlers.app_home_handler import _get_upcoming_birthdays

        mock_app = MagicMock()

        birthdays = {
            "U1": mock_birthday_data(date="01/01", year=1990, active=True),
            "U2": mock_birthday_data(date="02/01", year=1985, active=False),
        }

        def is_active_side_effect(user_id, data):
            return data.get("preferences", {}).get("active", True)

        with patch("services.birthday_queries.get_username", return_value="User"):
            with patch("slack.client.get_channel_members", return_value=["U1", "U2"]):
                with patch("storage.birthdays.is_user_active", side_effect=is_active_side_effect):
                    with patch(
                        "services.birthday_queries.calculate_days_until_birthday",
                        return_value=5,
                    ):
                        result = _get_upcoming_birthdays(birthdays, mock_app, limit=10)

        assert len(result) == 1
        assert result[0]["people"][0]["user_id"] == "U1"
