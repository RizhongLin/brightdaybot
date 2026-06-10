"""
Special Day Message Generator

AI-powered message generation for special days/holidays using OpenAI API.
Leverages the Chronicler personality by default but supports all personalities.
"""

from datetime import datetime
from typing import List, Optional

from config import (
    DESCRIPTION_TEASER_LENGTH,
    DIGEST_DESCRIPTION_LENGTH,
    GROUNDED_SPECIAL_DAYS_ENABLED,
    IMAGE_GENERATION_PARAMS,
    REASONING_EFFORT,
    SPECIAL_DAY_MENTION_ENABLED,
    SPECIAL_DAYS_IMAGE_ENABLED,
    SPECIAL_DAYS_PERSONALITY,
    TEAM_NAME,
    TEMPERATURE_SETTINGS,
    TOKEN_LIMITS,
    get_logger,
)
from config.personality import get_personality_config
from integrations.openai import complete
from integrations.web_search import get_birthday_facts
from services.image_generator import generate_birthday_image
from slack.emoji import get_emoji_context_for_ai
from utils.sanitization import markdown_to_slack_mrkdwn

# Get dedicated logger
logger = get_logger("special_days")


def _resolve_special_day_personality(personality_name, required_key):
    """Resolve personality, falling back to chronicler if required prompt key is missing."""
    personality = personality_name or SPECIAL_DAYS_PERSONALITY
    config = get_personality_config(personality)
    if required_key not in config and personality != "chronicler":
        logger.info(
            f"Personality {personality} doesn't have {required_key} prompts, using Chronicler"
        )
        personality = "chronicler"
        config = get_personality_config(personality)
    return personality, config


def _build_source_link(day):
    """Build a Slack-formatted source link from a SpecialDay object."""
    if hasattr(day, "source") and day.source:
        if hasattr(day, "url") and day.url:
            return f"<{day.url}|{day.source}>"
        return day.source
    return ""


def _fetch_facts_text(date_str, personality):
    """Fetch historical facts for a date, returning empty string on failure."""
    try:
        facts_result = get_birthday_facts(date_str, personality)
        if facts_result and facts_result.get("facts"):
            logger.info("Successfully retrieved historical facts for context")
            return facts_result["facts"]
    except Exception as e:
        logger.warning(f"Could not fetch web facts: {e}. Continuing without them.")
    return ""


def generate_special_day_message(
    special_days: List,
    personality_name: Optional[str] = None,
    include_facts: bool = True,
    test_mode: bool = False,
    suppress_mention: bool = False,
    app=None,
    use_teaser: bool = True,
    test_date=None,
) -> Optional[str]:
    """
    Generate an AI message for special day(s).

    Args:
        special_days: List of SpecialDay objects for today
        personality_name: Optional personality override (defaults to SPECIAL_DAYS_PERSONALITY)
        include_facts: Whether to include web search facts
        test_mode: Whether this is a test (affects token limits)
        app: Optional Slack app instance for custom emoji support
        use_teaser: If True, generate SHORT teaser (3-4 lines). If False, use full format (backward compat)
        test_date: Optional datetime object for testing specific dates (defaults to today)

    Returns:
        Generated message string or None if generation fails
    """
    if not special_days:
        logger.warning("No special days provided for message generation")
        return None

    personality, personality_config = _resolve_special_day_personality(
        personality_name, "special_day_single"
    )

    # Get emoji context for AI message generation (uses config default: 50)
    emoji_ctx = get_emoji_context_for_ai(app)
    emoji_examples = emoji_ctx["emoji_examples"]

    try:
        # Get current date in European format for organic inclusion
        # Use test_date if provided (for testing specific dates), otherwise use today
        from utils.date_utils import format_date_european

        today = test_date if test_date else datetime.now()
        today_formatted = format_date_european(today)  # e.g., "15 April 2025"
        day_of_week = today.strftime("%A")  # e.g., "Monday"

        # Grounded mode: the official description carries the facts (quoted in
        # the announcement blocks); the AI only writes a short themed intro.
        # Only applies to single days that actually have a description.
        grounded = (
            GROUNDED_SPECIAL_DAYS_ENABLED
            and len(special_days) == 1
            and bool(getattr(special_days[0], "description", "") or "")
        )

        facts_text = (
            _fetch_facts_text(today.strftime("%d/%m"), personality)
            if include_facts and not grounded
            else ""
        )

        # Prepare the prompt based on number of special days
        if grounded:
            day = special_days[0]
            prompt = (
                f"Today ({today_formatted}, {day_of_week}) is {day.name}"
                f" ({day.category}). Write a short, lively 1-2 sentence intro in your"
                " personality's voice announcing it — maximum 250 characters."
                " Do NOT state facts, history, dates, or statistics about the"
                " observance: the official description is displayed right below"
                " your intro. Just set the tone and invite people to read on."
            )
            if suppress_mention:
                prompt += (
                    f"\n\nEMOJI OVERRIDE: Do NOT start with the observance emoji"
                    f" {day.emoji or ''} — it's already shown in the header."
                    f" Include 1-2 emojis naturally within the text instead."
                    f" Available emojis: {emoji_examples}"
                )
            else:
                prompt += (
                    f"\n\nEMOJI USAGE: Include 1-2 relevant emojis."
                    f" Available emojis: {emoji_examples}"
                )
        elif len(special_days) == 1:
            day = special_days[0]

            source_info = _build_source_link(day)

            # Choose prompt based on use_teaser flag
            if use_teaser:
                prompt_key = "special_day_teaser"
                # For teasers, we don't need date/facts complexity
            else:
                prompt_key = "special_day_single"

            prompt = personality_config.get(
                prompt_key, personality_config.get("special_day_single", "")
            )

            # Fill in template variables including source
            prompt = prompt.format(
                day_name=day.name,
                category=day.category,
                description=(
                    day.description[:DESCRIPTION_TEASER_LENGTH] if use_teaser else day.description
                ),  # Truncate for teaser
                emoji=day.emoji or "",
                source=source_info if source_info else "UN/WHO observance",
            )

            if not use_teaser:
                # Add category-specific emphasis only for full messages
                category_emphasis = personality_config.get("special_day_category", {}).get(
                    day.category, ""
                )
                if category_emphasis:
                    prompt += f"\n\n{category_emphasis}"

                # Add date requirement for full messages
                prompt += f"\n\nTODAY'S DATE: {today_formatted} ({day_of_week})"
                prompt += "\n\nDATE REQUIREMENT: Organically mention today's date somewhere in your announcement. Examples:"
                prompt += f"\n- 'On {today_formatted}, we observe...'"
                prompt += f"\n- 'This {day_of_week}, {today_formatted}, marks...'"
                prompt += f"\n- '{today_formatted} brings us...'"
                prompt += "\nIntegrate naturally - keep it coherent and not forced."

            # Add emoji instructions
            emoji_count = "2-3" if use_teaser else "3-5"
            if suppress_mention:
                # Consolidated mode: emoji + name already in the header label
                prompt += f"\n\nEMOJI OVERRIDE: Do NOT start lines with the observance emoji {day.emoji if len(special_days) == 1 else ''} — it's already shown in the header. Include {emoji_count} emojis naturally within the text instead. Available emojis: {emoji_examples}"
            else:
                prompt += f"\n\nEMOJI USAGE: Include {emoji_count} relevant emojis throughout your message for visual appeal. Available emojis: {emoji_examples}"

        else:
            # Multiple special days
            days_list = ", ".join(
                [f"{d.emoji} {d.name}" if d.emoji else d.name for d in special_days]
            )

            # Choose prompt based on use_teaser flag
            if use_teaser:
                prompt_key = "special_day_multiple_teaser"
            else:
                prompt_key = "special_day_multiple"

            sources_info = []
            if not use_teaser:
                for day in special_days:
                    link = _build_source_link(day)
                    sources_info.append(f"{day.name}: {link or 'UN/WHO observance'}")

            sources_text = "\n".join(sources_info) if sources_info else "Various UN/WHO observances"

            prompt = personality_config.get(
                prompt_key, personality_config.get("special_day_multiple", "")
            )

            # Format with appropriate parameters
            if use_teaser:
                prompt = prompt.format(days_list=days_list, count=len(special_days))
            else:
                prompt = prompt.format(days_list=days_list, sources=sources_text)

            if not use_teaser:
                # Add date requirement for multiple days (full messages only)
                prompt += f"\n\nTODAY'S DATE: {today_formatted} ({day_of_week})"
                prompt += "\n\nDATE REQUIREMENT: Organically reference today's date when introducing the observances. Keep it natural."

            # Add emoji instructions
            emoji_count = "3-4" if use_teaser else "4-6"
            prompt += f"\n\nEMOJI USAGE: Include {emoji_count} emojis throughout your message{' (at least one per observance)' if not use_teaser else ''} for visual appeal. Available emojis: {emoji_examples}"

        # Add facts if available (only for full messages)
        if facts_text and not use_teaser:
            prompt += f"\n\nHistorical context for today: {facts_text}"

        # Add channel mention (conditional based on config; suppressed in consolidated mode)
        if SPECIAL_DAY_MENTION_ENABLED and not suppress_mention:
            prompt += "\n\nInclude <!here> to notify the channel."
        else:
            prompt += "\n\nDo NOT include <!here> or any channel mention."

        # Add character limit for teasers (grounded prompts carry their own limit)
        if use_teaser and not grounded:
            prompt += "\n\nSTRICT LENGTH LIMIT: Maximum 400 characters total. Be concise."

        # Generate the message using Responses API
        # Use lower token limit for teasers (shorter messages)
        max_tokens = 1000 if use_teaser else TOKEN_LIMITS.get("single_birthday", 2000)
        temperature = TEMPERATURE_SETTINGS.get("default", 0.7)

        logger.info(
            f"Generating special day {'teaser' if use_teaser else 'message'} with {personality} personality"
        )
        logger.debug(f"Prompt preview: {prompt[:200]}...")

        message = complete(
            messages=[
                {
                    "role": "system",
                    "content": f"You are {personality_config['name']}, {personality_config['description']} for the {TEAM_NAME} workspace.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=REASONING_EFFORT["analytical"],
            context="SPECIAL_DAY_MESSAGE",
        )
        if not message:
            logger.warning("SPECIAL_DAY: AI generated empty message, using fallback")
            return generate_fallback_special_day_message(special_days, personality_config)
        message = markdown_to_slack_mrkdwn(message.strip())

        logger.info("Successfully generated special day message")
        return message

    except Exception as e:
        logger.error(f"Error generating special day message: {e}")
        return generate_fallback_special_day_message(special_days, personality_config)


def generate_consolidated_intro_message(
    special_days: List,
    personality_name: Optional[str] = None,
    app=None,
) -> str:
    """
    Generate a short AI intro for consolidated special day announcements.

    Args:
        special_days: List of SpecialDay objects for today
        personality_name: Optional personality override
        app: Optional Slack app instance for custom emoji support

    Returns:
        Intro message string (2-3 lines)
    """
    personality = personality_name or SPECIAL_DAYS_PERSONALITY
    personality_config = get_personality_config(personality)

    count = len(special_days)
    names = [d.name if hasattr(d, "name") else d.get("name", "") for d in special_days]
    preview = ", ".join(names[:6])
    if len(names) > 6:
        preview += f" and {len(names) - 6} more"

    emoji_ctx = get_emoji_context_for_ai(app)
    emoji_examples = emoji_ctx["emoji_examples"]

    mention = (
        "Start with <!here> to notify the channel."
        if SPECIAL_DAY_MENTION_ENABLED
        else "Do NOT include <!here>."
    )

    try:
        prompt = f"""Generate a BRIEF 2-3 line intro for a consolidated special day announcement.

Today features {count} observance(s): {preview}

REQUIREMENTS:
- {mention}
- Be concise (2-3 lines max)
- Mention it's a day with multiple observances
- Invite readers to explore each one below
- Include 2-3 relevant emojis
- Use *single asterisks* for bold, _single underscores_ for italic

Available emojis: {emoji_examples}"""

        message = complete(
            messages=[
                {
                    "role": "system",
                    "content": f"You are {personality_config['name']}, {personality_config['description']} for the {TEAM_NAME} workspace.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1000,
            temperature=TEMPERATURE_SETTINGS.get("default", 0.7),
            reasoning_effort=REASONING_EFFORT["analytical"],
            context="CONSOLIDATED_INTRO_MESSAGE",
        )

        if message:
            logger.info("Successfully generated consolidated intro message")
            return markdown_to_slack_mrkdwn(message.strip())

    except Exception as e:
        logger.error(f"Error generating consolidated intro message: {e}")

    # Fallback
    mention_text = "<!here> " if SPECIAL_DAY_MENTION_ENABLED else ""
    return (
        f"{mention_text}📅 *Today marks {count} special observances!*\n\n"
        f"Featuring: {preview}\n"
        f"Read on for details about each one."
    )


def generate_fallback_special_day_message(special_days: List, personality_config: dict) -> str:
    """
    Generate a fallback message when AI generation fails.

    Args:
        special_days: List of SpecialDay objects
        personality_config: Personality configuration dictionary

    Returns:
        Fallback message string
    """
    mention = "<!here> " if SPECIAL_DAY_MENTION_ENABLED else ""

    if len(special_days) == 1:
        day = special_days[0]
        emoji = f"{day.emoji} " if day.emoji else ""
        message = "📅 TODAY IN HUMAN HISTORY...\n\n"
        message += f"Today marks {emoji}*{day.name}*!\n\n"
        message += f"{day.description}\n\n"
        message += f"{mention}Let's take a moment to recognize this important {day.category.lower()} observance.\n\n"
        message += f"- {personality_config.get('name', 'The Chronicler')}"
    else:
        message = "📅 TODAY IN HUMAN HISTORY...\n\n"
        message += "Today brings together multiple important observances:\n\n"
        for day in special_days:
            emoji = f"{day.emoji} " if day.emoji else ""
            message += f"• {emoji}*{day.name}* ({day.category})\n"
        message += f"\n{mention}These observances remind us of humanity's diverse priorities and shared values.\n\n"
        message += f"- {personality_config.get('name', 'The Chronicler')}"

    return message


def generate_weekly_digest_message(
    upcoming_days: dict,
    personality_name: Optional[str] = None,
    app=None,
) -> Optional[str]:
    """
    Generate an AI intro message for the weekly special days digest.

    Args:
        upcoming_days: Dict mapping date strings to lists of SpecialDay objects
        personality_name: Optional personality override (defaults to SPECIAL_DAYS_PERSONALITY)
        app: Optional Slack app instance for custom emoji support

    Returns:
        Generated intro message string or fallback if generation fails
    """
    # Get personality configuration
    personality = personality_name or SPECIAL_DAYS_PERSONALITY
    personality_config = get_personality_config(personality)

    # Count observances
    total_observances = sum(len(days) for days in upcoming_days.values())
    days_with_observances = len(upcoming_days)

    # Get emoji context for AI message generation
    emoji_ctx = get_emoji_context_for_ai(app)
    emoji_examples = emoji_ctx["emoji_examples"]

    # Build list of observance names for context
    observance_names = []
    for date_str, days in upcoming_days.items():
        for day in days:
            observance_names.append(day.name)

    # Limit to first 10 for prompt brevity
    if len(observance_names) > 10:
        observance_preview = (
            ", ".join(observance_names[:10]) + f" and {len(observance_names) - 10} more"
        )
    else:
        observance_preview = ", ".join(observance_names)

    try:
        prompt = f"""Generate a BRIEF 2-3 line intro for a weekly special days digest announcement.

This week features {total_observances} observance(s) across {days_with_observances} day(s):
{observance_preview}

SLACK FORMATTING: Use *single asterisks* for bold, _single underscores_ for italic. Do NOT use **double asterisks** or __double underscores__. For links use <URL|text> format.

REQUIREMENTS:
- Start with <!here> to notify the channel
- Be concise (2-3 lines max)
- Mention it's the "weekly digest" or "week ahead"
- Optional: briefly reference 1-2 notable observances from the list
- End with something inviting people to review the full list below
- Include 2-3 relevant emojis for visual appeal

Available emojis: {emoji_examples}

TONE: Informative but not overwhelming. This is a summary, not a detailed announcement."""

        message = complete(
            messages=[
                {
                    "role": "system",
                    "content": f"You are {personality_config['name']}, {personality_config['description']} for the {TEAM_NAME} workspace.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1000,
            temperature=TEMPERATURE_SETTINGS.get("default", 0.7),
            reasoning_effort=REASONING_EFFORT["analytical"],
            context="WEEKLY_DIGEST_MESSAGE",
        )

        if not message:
            logger.warning("WEEKLY_DIGEST: AI generated empty message, using fallback")
            raise ValueError("Empty AI response")
        logger.info("Successfully generated weekly digest intro message")
        return markdown_to_slack_mrkdwn(message.strip())

    except Exception as e:
        logger.error(f"Error generating weekly digest message: {e}")
        # Return fallback message
        mention = "<!here> " if SPECIAL_DAY_MENTION_ENABLED else ""
        return (
            f"{mention}📅 *Weekly Special Days Digest*\n\n"
            f"Here's what's coming up this week: {total_observances} observance(s) across {days_with_observances} day(s).\n\n"
            f"- {personality_config.get('name', 'The Chronicler')}"
        )


def generate_digest_descriptions(special_days: List) -> dict:
    """
    Generate concise one-line descriptions for special days in a weekly digest.

    Uses a single batch AI call to generate or shorten descriptions for all
    observances at once. Falls back to truncated existing descriptions on failure.

    Args:
        special_days: Flat list of SpecialDay objects for the week

    Returns:
        Dict mapping observance name to short description string
    """
    if not special_days:
        return {}

    # Build input lines for the AI prompt
    input_lines = []
    for day in special_days:
        name = day.name if hasattr(day, "name") else day.get("name", "")
        desc = day.description if hasattr(day, "description") else day.get("description", "")
        if desc:
            input_lines.append(f"- {name}: {desc}")
        else:
            input_lines.append(f"- {name}: (no description)")

    try:
        max_chars = DIGEST_DESCRIPTION_LENGTH
        prompt = f"""For each observance below, write a concise one-line description (max {max_chars} characters).
If a description is provided, shorten it. If missing, create one from the name.
Return EXACTLY one line per observance in the format: Name: description

{chr(10).join(input_lines)}"""

        result = complete(
            messages=[
                {
                    "role": "system",
                    "content": "You generate concise, informative one-line descriptions for international observances and holidays. Be factual and direct.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=TOKEN_LIMITS.get("digest_descriptions", 400),
            temperature=TEMPERATURE_SETTINGS.get("factual", 0.3),
            context="DIGEST_DESCRIPTIONS",
        )

        # Parse AI response into dict
        descriptions = {}
        if result:
            for line in result.strip().split("\n"):
                line = line.strip().lstrip("- ")
                if ":" in line:
                    name_part, desc_part = line.split(":", 1)
                    descriptions[name_part.strip()] = desc_part.strip()[:max_chars]

        logger.info(f"Generated {len(descriptions)} digest descriptions via AI")
        return descriptions

    except Exception as e:
        logger.error(f"Error generating digest descriptions: {e}")

    # Fallback: truncate existing descriptions
    fallback = {}
    for day in special_days:
        name = day.name if hasattr(day, "name") else day.get("name", "")
        desc = day.description if hasattr(day, "description") else day.get("description", "")
        if desc:
            fallback[name] = desc[:DIGEST_DESCRIPTION_LENGTH]
    return fallback


def generate_special_day_details(
    special_days: List,
    personality_name: Optional[str] = None,
    app=None,
    test_date=None,
) -> Optional[str]:
    """
    Generate DETAILED content for special day(s) - used for "View Details" button.

    Args:
        special_days: List of SpecialDay objects
        personality_name: Optional personality override (defaults to SPECIAL_DAYS_PERSONALITY)
        app: Optional Slack app instance for custom emoji support
        test_date: Optional datetime object for testing specific dates (defaults to today)

    Returns:
        Detailed message string or None if generation fails
    """
    if not special_days:
        logger.warning("No special days provided for details generation")
        return None

    personality, personality_config = _resolve_special_day_personality(
        personality_name, "special_day_details"
    )

    # Get emoji context for AI message generation
    emoji_ctx = get_emoji_context_for_ai(app)
    emoji_examples = emoji_ctx["emoji_examples"]

    # Get today's date for web search
    # Use test_date if provided (for testing specific dates), otherwise use today
    today = test_date if test_date else datetime.now()

    try:
        # For single special day
        if len(special_days) == 1:
            day = special_days[0]

            source_info = _build_source_link(day)
            facts_text = _fetch_facts_text(today.strftime("%d/%m"), personality)

            prompt = personality_config.get("special_day_details", "")

            # Fill in template variables
            prompt = prompt.format(
                day_name=day.name,
                category=day.category,
                description=day.description,
                emoji=day.emoji or "",
                source=source_info if source_info else "UN/WHO observance",
            )

            # Add emoji instructions
            prompt += f"\n\nEMOJI USAGE: Include 6-8 relevant emojis throughout for visual appeal. Available emojis: {emoji_examples}"

            # Add historical facts if available to provide real-world context
            if facts_text:
                prompt += f"\n\nADDITIONAL CONTEXT: Historical events on this date that may provide relevant context:\n{facts_text}\n\nYou may reference these if they connect meaningfully to the observance, but they are not required."

        else:
            # Multiple special days - generate AI-powered combined detailed content
            # Build comprehensive context for all observances
            observances_context = []
            for i, day in enumerate(special_days, 1):
                link = _build_source_link(day)
                source_info = f" (Source: {link})" if link else ""
                observances_context.append(
                    f"{i}. {day.emoji} *{day.name}* ({day.category}): {day.description}{source_info}"
                )

            observances_text = "\n\n".join(observances_context)

            facts_text = _fetch_facts_text(today.strftime("%d/%m"), personality)

            # Create prompt for multiple observances
            # Calculate proportional length based on number of observances
            if len(special_days) == 2:
                length_guidance = "16-22 lines"
            elif len(special_days) >= 3:
                length_guidance = "20-28 lines"
            else:
                length_guidance = "14-20 lines"

            prompt = f"""Generate engaging, detailed content for {len(special_days)} special day observances happening today.

SLACK FORMATTING:
- Use *single asterisks* for bold, _single underscores_ for italic
- Combine: *_bold italic_* (asterisks outside, underscores inside)
- For links: <URL|text> format. NEVER use [text](url) or HTML tags
- Use 🔹 for bullet lists. Include 8-12 emojis naturally throughout.

Available emojis: {emoji_examples}

OBSERVANCES TODAY:
{observances_text}

STRUCTURE ({length_guidance} total):

*_Common Thread:_*
[1-2 sentences weaving all {len(special_days)} observances together thematically — find the human connection.]

Then for EACH observance:

*_[Observance Name]:_*

*_Why It Matters:_*
[2-3 sentences: when/why established, who championed it, and what it represents today. Storytelling, not reporting.]

*_Key Facts:_*
🔹 [Surprising or compelling fact]
🔹 [Human-scale detail — how this touches real lives]

*_Take Action:_*
💡 [One specific personal action]
👥 [One team-level idea]

LENGTH: MAXIMUM {length_guidance}. Be vivid but concise — every line should earn its place.

HONESTY:
- Use ONLY facts from provided descriptions and sources
- Qualify general knowledge with "typically," "often," "generally"
- DO NOT fabricate statistics, dates, or numbers

PROHIBITIONS:
- DO NOT add "Learn More" or "Official Source" sections (handled by buttons)
- DO NOT include URLs (source links added automatically)
- DO NOT use **double asterisks** or __double underscores__
- DO NOT add a title/header (added automatically)

TONE: Chronicler personality — keeper of human history, warm yet dignified. Connect observances to broader human progress."""

            if facts_text:
                prompt += f"\n\nADDITIONAL CONTEXT: Historical events on this date that may provide relevant context:\n{facts_text}\n\nYou may reference these if they connect meaningfully to the observances, but they are not required."

        # Generate the detailed message using Responses API
        # Use appropriate token limit based on number of observances
        if len(special_days) == 1:
            max_tokens = TOKEN_LIMITS.get(
                "special_day_details", 600
            )  # Single observance (10-14 lines)
        else:
            max_tokens = TOKEN_LIMITS.get(
                "special_day_details_consolidated", 1000
            )  # Multiple observances (12-18 lines)

        temperature = TEMPERATURE_SETTINGS.get("default", 0.7)

        logger.info(f"Generating special day details with {personality} personality")

        details = complete(
            messages=[
                {
                    "role": "system",
                    "content": f"You are {personality_config['name']}, {personality_config['description']} for the {TEAM_NAME} workspace.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            context="SPECIAL_DAY_DETAILS",
            reasoning_effort=REASONING_EFFORT["analytical"],
        )
        if not details:
            logger.warning("SPECIAL_DAY_DETAILS: AI generated empty response")
            return None
        details = markdown_to_slack_mrkdwn(details.strip())

        logger.info(f"Successfully generated special day details ({len(details)} chars)")
        return details

    except Exception as e:
        logger.error(f"Error generating special day details: {e}")
        # Fallback to simple description
        if len(special_days) == 1:
            day = special_days[0]
            return f"📖 *{day.name} - Details*\n\n{day.description}"
        else:
            message = "📖 *Today's Special Observances*\n\n"
            for day in special_days:
                message += f"• *{day.name}*: {day.description}\n\n"
            return message.strip()


def generate_special_day_image(
    special_days: List,
    personality_name: Optional[str] = None,
    quality: Optional[str] = None,
    size: Optional[str] = None,
    test_mode: bool = False,
) -> Optional[str]:
    """
    Generate an AI image for special day(s).

    Args:
        special_days: List of SpecialDay objects
        personality_name: Optional personality override
        quality: Image quality (low/medium/high/auto)
        size: Image size (auto/1024x1024/1536x1024/1024x1536)
        test_mode: Whether this is a test

    Returns:
        Path to generated image or None
    """
    if not SPECIAL_DAYS_IMAGE_ENABLED and not test_mode:
        logger.info("Special days image generation is disabled")
        return None

    if not special_days:
        return None

    try:
        # Get personality configuration
        personality = personality_name or SPECIAL_DAYS_PERSONALITY
        personality_config = get_personality_config(personality)

        # Build image prompt
        if len(special_days) == 1:
            day = special_days[0]
            base_prompt = personality_config.get("image_prompt", "")

            # Format the prompt with special day info
            image_prompt = base_prompt.format(
                day_name=day.name,
                category=day.category,
                message_context=f" Theme: {day.description}" if day.description else "",
            )
        else:
            # Multiple days - create composite image prompt
            days_names = ", ".join([d.name for d in special_days])
            categories = list(set([d.category for d in special_days]))

            image_prompt = (
                f"An artistic composition representing multiple observances: {days_names}. "
            )
            image_prompt += f"Blend elements from these categories: {', '.join(categories)}. "
            image_prompt += "Educational poster style with symbols representing each observance. "
            image_prompt += "Dignified, informative, and visually balanced composition."

        # Use quality and size parameters
        if not quality:
            quality = IMAGE_GENERATION_PARAMS["quality"]["test" if test_mode else "default"]
        if not size:
            size = IMAGE_GENERATION_PARAMS["size"]["default"]

        logger.info(f"Generating special day image with {personality} style")

        # Generate using text-only mode (no face reference for special days)
        result = generate_birthday_image(
            name=special_days[0].name if len(special_days) == 1 else "Special Days",
            birthday_message=None,
            custom_image_prompt=image_prompt,
            personality_name=personality,
            quality=quality,
            image_size=size,
            test_mode=test_mode,
            use_reference_photo=False,  # Special days don't use face references
        )

        if result and "image_path" in result:
            logger.info(f"Successfully generated special day image: {result['image_path']}")
            return result["image_path"]

        return None

    except Exception as e:
        logger.error(f"Error generating special day image: {e}")
        return None


# Test function
if __name__ == "__main__":
    from storage.special_days import get_todays_special_days

    print("Testing Special Day Message Generator...")

    # Get today's special days
    special_days = get_todays_special_days()

    if special_days:
        print(f"\nFound {len(special_days)} special day(s) for today")

        # Generate message
        message = generate_special_day_message(special_days, test_mode=True)
        print(f"\nGenerated Message:\n{message}")

        # Generate image path (won't actually create image in test)
        image_path = generate_special_day_image(special_days, test_mode=True)
        print(f"\nWould generate image at: {image_path}")
    else:
        print("\nNo special days found for today")

        # Create a test special day
        from storage.special_days import SpecialDay

        test_day = SpecialDay(
            date="03/14",
            name="Pi Day",
            category="Tech",
            description="Celebrating the mathematical constant π",
            emoji="🥧",
            enabled=True,
        )

        print("\nTesting with Pi Day...")
        message = generate_special_day_message([test_day], test_mode=True)
        print(f"\nGenerated Message:\n{message}")
