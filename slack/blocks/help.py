"""Block Kit builders for help messages, welcome screens, and error responses."""

from typing import Any, Dict, List

from config import (
    DM_BIRTHDAY_SETUP_MODE,
    MENTION_QA_ENABLED,
    NLP_DATE_PARSING_ENABLED,
    UPCOMING_DAYS_DEFAULT,
    UPCOMING_DAYS_EXTENDED,
)
from config.personality import get_personality_descriptions


def _dm_setup_active() -> bool:
    """DM birthday setup is being phased out; hide DM date-entry hints once disabled."""
    return DM_BIRTHDAY_SETUP_MODE != "disabled"


def get_special_days_help_text() -> str:
    """Shared help text for special days admin commands. Used by both block help and inline help."""
    return """• `admin special list [category]` - List all observances (all sources)
• `admin special add/remove` - Manage custom days
• `admin special categories` - Manage category enable/disable
• `admin special test [DD/MM]` - Test announcement
• `admin special config` - View/update configuration
• `admin special verify` - Verify data accuracy
• `admin special mode [daily|weekly [day]]` - Announcement mode
• `admin special observances` - Combined source status
• `admin special [un|unesco|who]-status` - Individual cache status
• `admin special [un|unesco|who]-refresh` - Force refresh individual source
• `admin special all-refresh` - Refresh all observance sources
• `admin special calendarific-status` - All sources and status
• `admin special calendarific-refresh [source_id] [force]` - Prefetch all or one source
• `admin special calendarific-toggle <source_id>` - Enable/disable a source
• `admin special calendarific-emojis [source_id]` - Assign emojis via AI
• `admin special ics-list` - List ICS feed subscriptions
• `admin special ics-add <url> "Label" [category] [emoji]` - Subscribe to feed
• `admin special ics-remove <id>` - Remove subscription
• `admin special ics-toggle <id>` - Enable/disable subscription
• `admin special ics-refresh [id]` - Refresh feed(s)
• `admin special ics-test <url>` - Preview feed without subscribing"""


def build_welcome_blocks(
    user_mention: str, channel_mention: str
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for welcome message when user joins birthday channel

    Args:
        user_mention: Formatted user mention (e.g., "<@U123456>")
        channel_mention: Formatted channel mention (e.g., "<#C123456|birthdays>")

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🎉 Welcome {user_mention}!",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Welcome to {channel_mention}! Here I celebrate everyone's birthdays with personalized AI messages and images.",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": "*📅 Add Your Birthday:*\nUse `/birthday` to open the form\nor visit my *App Home* tab",
                },
                {
                    "type": "mrkdwn",
                    "text": "*🏠 App Home:*\nVisit my Home tab to view your status, preferences, and upcoming events.",
                },
                {
                    "type": "mrkdwn",
                    "text": "*💡 Get Help:*\nType `help` in a DM to see all commands and options.",
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "🎂 Hope to celebrate your special day soon! Not interested? Use `/birthday pause` to opt out.",
                }
            ],
        },
    ]

    fallback_text = (
        f"🎉 Welcome to {channel_mention}, {user_mention}! Use `/birthday` to add your birthday."
    )

    return blocks, fallback_text


def build_hello_blocks(
    greeting: str, personality_name: str = "BrightDay"
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for hello/greeting messages

    Args:
        greeting: Personalized greeting from personality (e.g., "Hello @user! 👋")
        personality_name: Display name of current personality

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": greeting,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"I'm *{personality_name}*, your friendly birthday celebration bot! I help make everyone's special day memorable with personalized AI messages and images.",
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": "*📅 Get Started:*\nUse `/birthday` to add yours!\nOr visit my *App Home* tab.",
                },
                {
                    "type": "mrkdwn",
                    "text": "*💡 Need Help?*\nType `help` to see all commands and features",
                },
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "🎂 Hope to celebrate with you soon!"}],
        },
    ]

    fallback_text = f"{greeting}\n\nI'm {personality_name}, your birthday bot! Use /birthday to add yours, or type 'help' for more info."

    return blocks, fallback_text


def build_help_blocks(is_admin: bool = False) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for help messages

    Args:
        is_admin: If True, show admin help; otherwise show user help

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    blocks = []

    if is_admin:
        # Admin help - comprehensive command reference with organized sections
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🔧 Admin Commands Reference"},
            }
        )

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Complete admin command reference organized by category. All commands require admin privileges.",
                },
            }
        )

        blocks.append({"type": "divider"})

        # --- Core Features ---

        # Birthday Management
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🎂 Birthday Management*"},
            }
        )
        birthday_mgmt = """• `list` - List upcoming birthdays
• `list all` - List all birthdays organized by month
• `admin stats` - View birthday statistics
• `admin remind` or `admin remind new` - Send reminders to users without birthdays
• `admin remind update` - Send profile update reminders
• `admin remind all` - Send reminders to both new and existing users
• `admin remind [type] [message]` - Custom reminder message"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": birthday_mgmt}})

        blocks.append({"type": "divider"})

        # Special Days Management
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🌟 Special Days Management*"},
            }
        )
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": get_special_days_help_text()}}
        )

        blocks.append({"type": "divider"})

        # Bot Personality
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🎭 Bot Personality*"},
            }
        )
        # Get personality list dynamically
        personality_names = get_personality_descriptions().keys()
        personality_list = ", ".join(f"`{p}`" for p in personality_names)

        personality = f"""• `admin personality` - Show current bot personality
• `admin personality [name]` - Change bot personality

*Available:* {personality_list}"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": personality}})

        blocks.append({"type": "divider"})

        # --- Configuration ---

        # AI Configuration
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🤖 AI Model Configuration*"},
            }
        )
        ai_config = """• `admin model` - Show current OpenAI model and configuration
• `admin model list` - List all supported OpenAI models
• `admin model set <model>` - Change to specified model
• `admin model reset` - Reset to default model
• `admin image-model` - Show current image model
• `admin image-model list` - List supported image models
• `admin image-model set <model>` - Change image model (e.g. gpt-image-2)
• `admin image-model reset` - Reset image model to default"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": ai_config}})

        blocks.append({"type": "divider"})

        # Timezone Configuration
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🌍 Timezone Configuration*"},
            }
        )
        timezone = """• `admin timezone` - View current timezone status
• `admin timezone enable` - Enable timezone-aware mode (hourly checks)
• `admin timezone disable` - Disable timezone-aware mode (daily check)
• `admin bot-celebration` - View bot self-celebration status
• `admin bot-celebration enable/disable` - Toggle bot birthday celebration"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": timezone}})

        blocks.append({"type": "divider"})

        # System Management
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*⚙️ System Management*"},
            }
        )
        system_mgmt = """• `admin status` - View system health and component status
• `admin status detailed` - View detailed system information
• `admin config` - View command permissions
• `admin config COMMAND true/false` - Change command permissions"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": system_mgmt}})

        blocks.append({"type": "divider"})

        # --- Operations ---

        # Canvas Dashboard
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*📊 Canvas Dashboard*"},
            }
        )
        canvas_cmds = """• `admin canvas` or `admin canvas status` - Dashboard status and backup info
• `admin canvas refresh` - Force immediate update (bypasses debounce)
• `admin canvas reset` - Delete and recreate canvas from scratch
• `admin canvas clean` - Remove bot messages from ops channel (keeps backup thread)
• `admin canvas dismiss-warnings` - Dismiss all canvas warnings"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": canvas_cmds}})

        blocks.append({"type": "divider"})

        # Data Management
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*💾 Data Management*"},
            }
        )
        data_mgmt = """• `admin backup` - Create a manual backup of birthdays data
• `admin restore latest` - Restore from the latest backup
• `admin cache clear` - Clear all web search cache
• `admin cache clear DD/MM` - Clear web search cache for specific date"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": data_mgmt}})

        blocks.append({"type": "divider"})

        # Announcements
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*📣 Announcements*"},
            }
        )
        announcements = """• `admin announce image` - Announce AI image generation feature
• `admin announce [message]` - Send custom announcement to birthday channel
_(All announcements require confirmation)_"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": announcements}})

        blocks.append({"type": "divider"})

        # --- Development & Admin ---

        # Testing Commands
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🧪 Testing Commands*"},
            }
        )
        testing = """• `admin test @user1 [@user2...] [quality] [size] [--text-only]` - Test birthday message/images
• `admin test-join [@user]` - Test birthday channel welcome
• `admin test-bot-celebration [quality] [size] [--text-only]` - Test bot self-celebration
• `admin test-block [type]` - Test Block Kit rendering
• `admin test-upload` - Test image upload functionality
• `admin test-upload-multi` - Test multiple image attachments
• `admin test-blockkit [mode]` - Test Block Kit image embedding
• `admin test-file-upload` - Test text file upload
• `admin test-external-backup` - Test canvas backup system"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": testing}})

        blocks.append({"type": "divider"})

        # Admin Management
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*👥 Admin Management*"},
            }
        )
        admin_mgmt = """• `admin list` - List configured admin users
• `admin add USER_ID` - Add a user as admin
• `admin remove USER_ID` - Remove a user from admin list"""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": admin_mgmt}})

        # Footer
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 Most destructive commands require confirmation. Use `confirm` to proceed with pending actions.",
                    }
                ],
            }
        )

        fallback_text = (
            "Admin Commands Reference - Complete list of admin commands organized by category"
        )

    else:
        # User help - friendly and organized
        blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "💡 How to Use BrightDay"},
            }
        )

        blocks.append({"type": "divider"})

        # Quick Start Section
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📅 Quick Start*"}})

        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": "*Slash Commands (preferred):*\n`/birthday` - Open birthday form\n`/special-day` - Today's observances",
                    },
                    {
                        "type": "mrkdwn",
                        "text": (
                            (
                                "*DM Shortcut:*\nSend `25/12`, `25/12/1990`,\n"
                                "or just write it out — `July 14th` works too"
                                if NLP_DATE_PARSING_ENABLED
                                else "*DM Shortcut:*\nSend `25/12` or `25/12/1990`\nto add your birthday directly"
                            )
                            if _dm_setup_active()
                            else "*App Home:*\nOpen my *Home* tab to set\nyour birthday and preferences"
                        ),
                    },
                ],
            }
        )

        blocks.append({"type": "divider"})

        # Birthday Commands
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🎂 Birthday Commands*"},
            }
        )

        add_command = "`/birthday` or `add DD/MM`" if _dm_setup_active() else "`/birthday`"
        birthday_commands = f"""• {add_command} - Add or update your birthday
• `/birthday check [@user]` or `check` - Check a birthday
• `/birthday list` or `list` - Upcoming birthdays
• `/birthday export` - Export birthdays to calendar (ICS)
• `remove` - Remove your birthday
• `pause` / `resume` - Pause or resume your celebrations
• `test [quality] [size] [--text-only]` - Preview your birthday message"""

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": birthday_commands}})

        blocks.append({"type": "divider"})

        # Special Days Commands
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🌍 Special Days Commands*"},
            }
        )

        special_commands = f"""• `/special-day` or `special` - Today's observances
• `/special-day week` or `special week` - Next {UPCOMING_DAYS_DEFAULT} days
• `/special-day month` or `special month` - Next {UPCOMING_DAYS_EXTENDED} days
• `/special-day export [source]` - Export to calendar (ICS)
• `special list [category]` - List all special days
• `special stats` - View statistics"""

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": special_commands}})

        blocks.append({"type": "divider"})

        # Other Commands
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*⚙️ Other*"},
            }
        )

        other_commands = """• `help` - Show this help message
• `hello` - Get a friendly greeting
• `admin help` - View admin commands _(if admin)_"""

        if MENTION_QA_ENABLED:
            other_commands = (
                "• `@BrightDay [question]` - Mention me in a channel to ask about "
                "birthdays, special days, or what I can do\n" + other_commands
            )

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": other_commands}})

        # Footer
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 Tip: Use `/birthday` in any channel — no need to DM the bot!",
                    }
                ],
            }
        )

        fallback_text = "BrightDay Help - Use /birthday to add your birthday or /special-day to see today's observances. Use 'admin help' for admin commands."

    return blocks, fallback_text


def build_unrecognized_input_blocks() -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for unrecognized DM input

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🤔 I Didn't Understand That"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "I didn't recognize a valid date format or command in your message.",
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": (
                        (
                            "*To add your birthday:*\nSend: `DD/MM`, `DD/MM/YYYY`,\n"
                            "or plain English like `July 14th`"
                            if NLP_DATE_PARSING_ENABLED
                            else "*To add your birthday:*\nSend: `DD/MM` or `DD/MM/YYYY`\nExample: `25/12` or `25/12/1990`"
                        )
                        if _dm_setup_active()
                        else "*To add your birthday:*\nUse `/birthday` (quick form)\nor my *Home* tab"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": "*For help:*\nType: `help`\nSee all available commands",
                },
            ],
        },
    ]

    fallback_text = "I didn't recognize a valid date format or command. Please send your birthday as DD/MM or type 'help' for more options."

    return blocks, fallback_text


def build_slash_help_blocks(
    command_type: str,
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build help blocks for slash commands.

    Args:
        command_type: "birthday" or "special-day"

    Returns:
        Tuple of (blocks, fallback_text)
    """
    if command_type == "birthday":
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "/birthday Command Help"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Available subcommands:*\n\n"
                    + "- `/birthday` or `/birthday add` - Open birthday form\n"
                    + "- `/birthday check [@user]` - Check birthday\n"
                    + "- `/birthday list` - List upcoming birthdays\n"
                    + "- `/birthday export` - Export birthdays to calendar (ICS)\n"
                    + "- `/birthday pause` - Pause your celebrations\n"
                    + "- `/birthday resume` - Resume your celebrations\n"
                    + "- `/birthday help` - Show this help",
                },
            },
        ]
        if MENTION_QA_ENABLED:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "💬 You can also mention me in a channel — `@BrightDay when is the next birthday?`",
                        }
                    ],
                }
            )
        fallback = "/birthday Command Help"
    else:
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "/special-day Command Help"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Available options:*\n\n"
                    + "- `/special-day` or `/special-day today` - Today's observances\n"
                    + f"- `/special-day week` - Next {UPCOMING_DAYS_DEFAULT} days\n"
                    + f"- `/special-day month` - Next {UPCOMING_DAYS_EXTENDED} days\n"
                    + "- `/special-day list [category]` - List all special days\n"
                    + "- `/special-day stats` - View statistics\n"
                    + "- `/special-day export [source]` - Export to calendar (ICS)\n"
                    + "- `/special-day help` - Show this help",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_Sources: UN/WHO/UNESCO observances, Calendarific holidays, and custom entries._",
                    }
                ],
            },
        ]
        fallback = "/special-day Command Help"

    return blocks, fallback
