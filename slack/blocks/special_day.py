"""
Special day-related Block Kit builders.

Handles daily announcements, weekly digests, list displays, and statistics.
Includes a file-backed cache for detailed content (avoids Slack button value limit,
survives bot restarts).
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from config import (
    GROUNDED_SPECIAL_DAYS_ENABLED,
    SLACK_SECTION_TEXT_MAX_LENGTH,
    SPECIAL_DAY_DETAILS_CACHE_FILE,
    SPECIAL_DAY_DETAILS_CACHE_TTL_DAYS,
    SPECIAL_DAY_QUOTE_MAX_CHARS,
    UPCOMING_DAYS_DEFAULT,
    UPCOMING_DAYS_EXTENDED,
)
from config.personality import get_personality_display_name


def _build_description_quote(day) -> Optional[str]:
    """
    Format a day's official description as a Slack quote, or None.

    Used in grounded mode: the AI intro sets the tone while the official
    source description (quoted) carries the facts.
    """
    if not GROUNDED_SPECIAL_DAYS_ENABLED:
        return None

    description = _get_attr(day, "description")
    if not description:
        return None

    quote = " ".join(str(description).split())  # collapse whitespace/newlines
    if len(quote) > SPECIAL_DAY_QUOTE_MAX_CHARS:
        quote = quote[:SPECIAL_DAY_QUOTE_MAX_CHARS].rstrip() + "…"
    return f"> {quote}"


# --- Details cache (JSON file-backed) ---
_details_cache_ttl = SPECIAL_DAY_DETAILS_CACHE_TTL_DAYS * 86400


def _load_details_cache():
    """Load the details cache from disk."""
    try:
        if os.path.exists(SPECIAL_DAY_DETAILS_CACHE_FILE):
            with open(SPECIAL_DAY_DETAILS_CACHE_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_details_cache(cache):
    """Save the details cache to disk atomically (write-to-temp-then-rename)."""
    try:
        cache_dir = os.path.dirname(SPECIAL_DAY_DETAILS_CACHE_FILE)
        os.makedirs(cache_dir, exist_ok=True)
        tmp_path = SPECIAL_DAY_DETAILS_CACHE_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp_path, SPECIAL_DAY_DETAILS_CACHE_FILE)
    except OSError:
        pass


def store_special_day_details(action_id, content, name=None, source=None, url=None):
    """Store detailed content for a special day View Details button (persisted to disk)."""
    store_special_day_details_batch(
        {action_id: {"content": content, "name": name, "source": source, "url": url}}
    )


def store_special_day_details_batch(entries: Dict[str, dict]):
    """Store multiple details entries in one file read/write cycle.

    Args:
        entries: Dict mapping action_id to {content, name, source, url}
    """
    if not entries:
        return
    cache = _load_details_cache()
    now = time.time()
    for action_id, data in entries.items():
        cache[action_id] = {**data, "stored_at": now}
    # Prune expired entries
    cache = {k: v for k, v in cache.items() if (now - v.get("stored_at", 0)) < _details_cache_ttl}
    _save_details_cache(cache)


def get_special_day_details(action_id):
    """Retrieve cached details by action_id. Returns dict or None if expired/missing."""
    cache = _load_details_cache()
    entry = cache.get(action_id)
    if not entry:
        return None
    if (time.time() - entry.get("stored_at", 0)) >= _details_cache_ttl:
        return None
    return entry


def _get_attr(obj, attr, default=None):
    """Get attribute from object or dict uniformly."""
    if hasattr(obj, attr):
        return getattr(obj, attr, default)
    elif isinstance(obj, dict):
        return obj.get(attr, default)
    return default


def build_special_day_blocks(
    special_days_or_name,  # Can be List[Any] or str (backward compat)
    message: str,
    observance_date: Optional[str] = None,  # Old signature param
    source: Optional[str] = None,  # Old signature param
    personality: str = "chronicler",
    detailed_content: Optional[str] = None,
    category: Optional[str] = None,  # Old signature param
    url: Optional[str] = None,  # Old signature param
    description: Optional[str] = None,  # Old signature param (deprecated)
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for special day announcements (single or multiple).

    Unified function that handles both single and multiple special day celebrations
    with appropriate headers and layouts.

    Supports two call patterns:
    1. New style: build_special_day_blocks([special_day1, special_day2], message, personality=..., detailed_content=...)
    2. Old style: build_special_day_blocks("World Health Day", message, "07/04", source="WHO", ...)

    Args:
        special_days_or_name: List of SpecialDay objects/dicts, OR observance name string (old style)
        message: AI-generated announcement message
        observance_date: (Old style only) Date of observance (DD/MM format)
        source: (Old style only) Source attribution
        personality: Bot personality name
        detailed_content: Optional AI-generated detailed content for "View Details" button
        category: (Old style only) Category
        url: (Old style only) Official URL
        description: DEPRECATED - use detailed_content instead

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    # Detect call style: list = new style, string = old style
    if isinstance(special_days_or_name, str):
        # Old style call - convert to new style
        special_days = [
            {
                "name": special_days_or_name,
                "date": observance_date,
                "source": source,
                "category": category,
                "url": url,
                "emoji": "🌍",
            }
        ]
        # Use description as fallback for detailed_content
        if not detailed_content and description:
            detailed_content = description
    else:
        # New style call - use list directly
        special_days = special_days_or_name

    if not special_days:
        return [], ""

    # Build header with specific emoji for the observance
    # Note: Always single day since we send separate announcements
    special_day = special_days[0]
    emoji = _get_attr(special_day, "emoji", "🌍") or "🌍"
    header_text = f"{emoji} {_get_attr(special_day, 'name', 'Special Day')}"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": message}},
    ]

    # Grounded mode: quote the official description as the factual core
    quote = _build_description_quote(special_day)
    if quote:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": quote}})

    # Add context block for metadata (date, source, and personality)
    context_elements = []

    # Get date
    date_str = _get_attr(special_day, "date")
    if date_str:
        from datetime import datetime

        from utils.date_utils import format_date_european_short

        try:
            date_obj = datetime.strptime(date_str, "%d/%m")
            formatted_date = format_date_european_short(date_obj)
        except ValueError:
            formatted_date = date_str

        context_elements.append({"type": "mrkdwn", "text": f"📅 *Date:* {formatted_date}"})

    # Display source
    source = _get_attr(special_day, "source")
    if source:
        context_elements.append({"type": "mrkdwn", "text": f"📋 *Source:* {source}"})

    # Add personality attribution
    personality_name = get_personality_display_name(personality)
    context_elements.append(
        {"type": "mrkdwn", "text": f"✨ _Brought to you by {personality_name}_"}
    )

    if context_elements:
        blocks.append({"type": "context", "elements": context_elements})

    # Add interactive buttons if detailed content or URL available
    url = _get_attr(special_day, "url")

    if detailed_content or url:
        actions = []
        name = _get_attr(special_day, "name", "Special Day")
        name_slug = name.lower().replace(" ", "_")[:20]

        # "View Details" button — store content in cache, button value is just the name
        # Include name slug in action_id to avoid collision when multiple days share a date
        if detailed_content:
            action_id = f"special_day_details_{date_str.replace('/', '_') if date_str else 'unknown'}_{name_slug}"
            store_special_day_details(
                action_id,
                detailed_content,
                name=name,
                source=source,
                url=url,
            )
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📖 View Details"},
                    "style": "primary",
                    "action_id": action_id,
                    "value": name,
                }
            )

        # "Official Source" button
        if url:
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔗 Official Source"},
                    "action_id": f"link_official_source_{date_str.replace('/', '_') if date_str else 'unknown'}_{name_slug}",
                    "url": url,
                }
            )

        if actions:
            blocks.append({"type": "actions", "elements": actions})

    # Generate fallback text
    fallback_text = f"{emoji} {_get_attr(special_day, 'name', 'Special Day')}"

    return blocks, fallback_text


def build_consolidated_special_day_blocks(
    special_days: List[Any],
    intro_message: str,
    teasers: Dict[str, str],
    detailed_contents: Dict[str, str],
    personality: str = "chronicler",
    observance_date: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for consolidated special day announcements.

    Groups all observances for a given date into one message, each with its own
    section (teaser, metadata, buttons) under a shared header.

    Args:
        special_days: List of SpecialDay objects/dicts for today
        intro_message: AI-generated intro with @-here mention
        teasers: Dict mapping observance name to AI teaser text
        detailed_contents: Dict mapping observance name to detailed content
        personality: Bot personality name
        observance_date: Date string in DD/MM format

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    from config import SLACK_MAX_BLOCKS, SPECIAL_DAY_COMPACT_THRESHOLD

    if not special_days:
        return [], ""

    count = len(special_days)
    compact = count >= SPECIAL_DAY_COMPACT_THRESHOLD
    personality_name = get_personality_display_name(personality)

    # Format date for header
    header_date = ""
    if observance_date:
        from datetime import datetime

        from utils.date_utils import format_date_european_short

        try:
            date_obj = datetime.strptime(observance_date, "%d/%m")
            header_date = f" — {format_date_european_short(date_obj)}"
        except ValueError:
            pass

    # Header + intro (matches single-day: header block → section block)
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📅 {count} Special Observances Today{header_date}",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": intro_message}},
    ]

    # Per-observance sections — mirrors single-day structure:
    #   section (name + teaser) → context (source + personality) → actions (buttons)
    cache_entries = {}  # Collect for batch write
    for idx, day in enumerate(special_days):
        name = _get_attr(day, "name", "Special Day")
        emoji = _get_attr(day, "emoji", "🌍") or "🌍"
        source = _get_attr(day, "source")
        url = _get_attr(day, "url")
        date_str = observance_date or _get_attr(day, "date")

        # Subsection header with numbering + teaser
        teaser = teasers.get(name, "")
        label = f"*{emoji}  {idx + 1}/{count} · {name}*"
        section_text = f"{label}\n{teaser}" if teaser else label

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": section_text}})

        # Grounded mode: quote the official description (skipped in compact
        # mode to stay within Slack's block limit)
        if not compact:
            quote = _build_description_quote(day)
            if quote:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": quote}})

        # Context block — matches single-day context (source + personality)
        if not compact:
            context_elements = []
            if source:
                context_elements.append({"type": "mrkdwn", "text": f"📋 *Source:* {source}"})
            context_elements.append(
                {"type": "mrkdwn", "text": f"✨ _Brought to you by {personality_name}_"}
            )
            blocks.append({"type": "context", "elements": context_elements})
        else:
            if source:
                blocks[-1]["text"]["text"] += f"\n📋 _Source: {source}_"

        # Buttons — collect cache entries for batch write
        details = detailed_contents.get(name, "")
        if details or url:
            actions = []
            if details:
                action_id = f"special_day_details_{date_str.replace('/', '_') if date_str else 'unknown'}_{idx}"
                cache_entries[action_id] = {
                    "content": details,
                    "name": name,
                    "source": source,
                    "url": url,
                }
                actions.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "📖 View Details"},
                        "style": "primary",
                        "action_id": action_id,
                        "value": name,
                    }
                )
            if url:
                actions.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🔗 Official Source"},
                        "action_id": f"link_official_source_{date_str.replace('/', '_') if date_str else 'unknown'}_{idx}",
                        "url": url,
                    }
                )
            if actions:
                blocks.append({"type": "actions", "elements": actions})

    # Closing divider + footer
    blocks.append({"type": "divider"})
    # Batch write all cached details in one file I/O cycle
    if cache_entries:
        store_special_day_details_batch(cache_entries)

    footer = f"📊 *{count} observance{'s' if count != 1 else ''}* today"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    # Safety: trim blocks to Slack limit
    if len(blocks) > SLACK_MAX_BLOCKS:
        blocks = blocks[: SLACK_MAX_BLOCKS - 1]
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_...and more. Use `/special-day` to see all {count} observances._",
                    }
                ],
            }
        )

    fallback_text = f"📅 {count} Special Observances Today{header_date}"
    return blocks, fallback_text


def build_weekly_special_days_blocks(
    upcoming_days: dict,
    intro_message: str,
    personality: str = "chronicler",
    descriptions: Optional[Dict[str, str]] = None,
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for weekly special days digest.

    Args:
        upcoming_days: Dict mapping date strings (DD/MM) to lists of SpecialDay objects
        intro_message: AI-generated intro message
        personality: Bot personality name
        descriptions: Optional dict mapping observance name to short description

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    from datetime import datetime

    from utils.date_utils import format_date_european_short

    if not upcoming_days:
        return [], "No special days this week"

    # Count totals
    total_observances = sum(len(days) for days in upcoming_days.values())
    days_with_observances = len(upcoming_days)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📅 Weekly Special Days Digest"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": intro_message}},
        {"type": "divider"},
    ]

    # Sort dates chronologically
    today = datetime.now()
    sorted_dates = []
    for date_str in upcoming_days.keys():
        try:
            date_obj = datetime.strptime(date_str, "%d/%m")
            # Set to current year for proper sorting
            date_obj = date_obj.replace(year=today.year)
            sorted_dates.append((date_str, date_obj))
        except ValueError:
            sorted_dates.append((date_str, today))

    sorted_dates.sort(key=lambda x: x[1])

    # Build sections for each day with observances
    for date_str, date_obj in sorted_dates:
        special_days = upcoming_days[date_str]

        # Format date header with day name
        day_name = date_obj.strftime("%A")
        formatted_date = format_date_european_short(date_obj)

        # Check if it's today
        is_today = date_obj.date() == today.date()
        day_label = f"*{day_name}, {formatted_date}*" + (" (Today)" if is_today else "")

        # Build observance list for this day
        observance_lines = []
        for day in special_days:
            emoji = _get_attr(day, "emoji", "🌍") or "🌍"
            name = _get_attr(day, "name", "Special Day")
            url = _get_attr(day, "url", "")
            source = _get_attr(day, "source", "")

            # Hyperlink the name if URL available
            name_text = f"<{url}|{name}>" if url else name

            # Add short description if available
            desc = (descriptions or {}).get(name, "")
            desc_text = f" — {desc}" if desc else ""

            source_text = f" _({source})_" if source else ""
            observance_lines.append(f"• {emoji} {name_text}{desc_text}{source_text}")

        observances_text = "\n".join(observance_lines)

        # Add day section
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{day_label}\n{observances_text}"},
            }
        )

    # Add footer with totals and personality
    blocks.append({"type": "divider"})

    personality_name = get_personality_display_name(personality)
    footer_text = (
        f"📊 *{total_observances} observance{'s' if total_observances != 1 else ''}* across *{days_with_observances} day{'s' if days_with_observances != 1 else ''}* this week\n"
        f"✨ _Brought to you by {personality_name}_\n\n"
        f"_Use `/special-day` for details on any specific observance._"
    )

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer_text}]})

    # Generate fallback text
    fallback_text = f"Weekly Special Days Digest: {total_observances} observance{'s' if total_observances != 1 else ''} this week"

    return blocks, fallback_text


def build_special_days_list_blocks(
    special_days: List[Any],
    view_mode: str = "list",
    category_filter: Optional[str] = None,
    date_filter: Optional[str] = None,
    admin_view: bool = False,
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for special days list display

    Args:
        special_days: List of SpecialDay objects
        view_mode: Display mode ("list", "today", "week", "month", "search")
        category_filter: Optional category filter for list view
        date_filter: Optional date string for today/week/month views
        admin_view: If True, show additional admin details (source, URL, status)

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    # Build header based on view mode
    if view_mode == "today":
        header_text = f"📅 Today's Special Days{f' ({date_filter})' if date_filter else ''}"
    elif view_mode == "week":
        header_text = "📅 Special Days - Next 7 Days"
    elif view_mode == "month":
        header_text = "📅 Special Days - Next 30 Days"
    elif view_mode == "search":
        header_text = "📅 Special Days Search Results"
    else:  # list
        if admin_view:
            header_text = (
                f"📅 Admin Special Days View{f' ({category_filter})' if category_filter else ''}"
            )
        elif category_filter:
            header_text = f"📅 All Special Days ({category_filter})"
        else:
            header_text = "📅 All Special Days"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text,
            },
        }
    ]

    # No special days found
    if not special_days:
        no_results_msg = {
            "today": "No special days observed today.",
            "week": f"No special days in the next {UPCOMING_DAYS_DEFAULT} days.",
            "month": f"No special days in the next {UPCOMING_DAYS_EXTENDED} days.",
            "search": "No special days found matching your search.",
            "list": f"No special days found{f' for category {category_filter}' if category_filter else ''}.",
        }

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": no_results_msg.get(view_mode, "No special days found."),
                },
            }
        )
        return blocks, f"{header_text}: None found"

    # Format special days based on view mode
    if view_mode in ["today", "search"]:
        # Simple list with status indicators
        for day in special_days:
            emoji_str = f"{day.emoji} " if day.emoji else ""
            status = "✅" if day.enabled else "❌"

            # Build main text
            day_text = f"{status} {emoji_str}*{day.name}*"
            if view_mode == "search":
                day_text += f" ({day.date})"

            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": day_text}})

            # Add description as context if available
            if day.description:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"_{day.description}_\n• Category: {day.category}",
                            }
                        ],
                    }
                )

    elif view_mode in ["week", "month"]:
        # Group by date, similar to current format
        # special_days is expected to be a dict like {date_str: [day1, day2]}
        if isinstance(special_days, dict):
            for date_str, days_list in special_days.items():
                # Date header
                date_text = f"*{date_str}:*\n"
                for day in days_list:
                    emoji = f"{day.emoji} " if day.emoji else ""
                    date_text += f"  • {emoji}{day.name}"
                    if view_mode == "month":
                        # For month view, just show name (more compact)
                        date_text += "\n"
                    else:
                        # For week view, add category
                        date_text += f" ({day.category})\n"

                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": date_text}})
        else:
            # Fallback if not a dict (shouldn't happen)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⚠️ Data format error. Please contact an admin.",
                    },
                }
            )

    elif view_mode == "list":
        # Group by month for better organization
        from calendar import month_name as cal_month_name
        from datetime import datetime

        from config import DATE_FORMAT

        months_dict = {}
        for day in special_days:
            try:
                date_obj = datetime.strptime(day.date, DATE_FORMAT)
                month_num = date_obj.month
            except ValueError:
                continue  # Skip invalid dates
            m_name = cal_month_name[month_num]
            if m_name not in months_dict:
                months_dict[m_name] = []
            months_dict[m_name].append(day)

        # Sort months chronologically (month_name[1] to month_name[12])
        for month in list(cal_month_name)[1:]:  # Skip empty first element
            if month in months_dict:
                # Month header
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*{month}*"},
                    }
                )

                # Sort days within month by date using datetime
                def get_day_sort_key(d):
                    try:
                        return datetime.strptime(d.date, DATE_FORMAT).day
                    except ValueError:
                        return 0

                months_dict[month].sort(key=get_day_sort_key)

                # Build month entries (split into chunks to avoid 3000 char limit)
                month_text = ""
                for day in months_dict[month]:
                    emoji = f"{day.emoji} " if day.emoji else ""

                    if admin_view:
                        # Admin view: show status, source, and URL
                        status = "✅" if day.enabled else "❌"
                        source = f"[{day.source}]" if day.source else "[Custom]"
                        entry = f"• {status} {day.date}: {emoji}*{day.name}* ({day.category}) {source}\n"
                        if day.url:
                            entry += f"  🔗 <{day.url}|View source>\n"
                    else:
                        # User view: simple format with bullet points
                        entry = f"• {emoji}{day.date} - {day.name}\n"

                    # Check if adding this entry would exceed Slack section text limit
                    if len(month_text) + len(entry) > SLACK_SECTION_TEXT_MAX_LENGTH:
                        # Flush current text and start new block
                        blocks.append(
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": month_text},
                            }
                        )
                        month_text = entry
                    else:
                        month_text += entry

                # Add remaining text
                if month_text:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": month_text},
                        }
                    )

    # Add context footer
    total_count = (
        len(special_days)
        if isinstance(special_days, list)
        else sum(len(days) for days in special_days.values())
    )
    context_text = f"📊 Total: {total_count} special day{'s' if total_count != 1 else ''}"
    if category_filter:
        context_text += f" in {category_filter}"

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": context_text}],
        }
    )

    # Add admin action hints
    if admin_view and view_mode == "list":
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_Actions: `admin special remove DD/MM` | `admin special test DD/MM` | `admin special add ...`_",
                    }
                ],
            }
        )

    # Fallback text
    fallback_text = f"{header_text}: {total_count} special days"

    return blocks, fallback_text


def build_special_day_stats_blocks(
    stats: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], str]:
    """
    Build Block Kit structure for special days statistics

    Args:
        stats: Statistics dictionary with total_days, enabled_days, feature_enabled,
               current_personality, and by_category breakdown

    Returns:
        Tuple of (blocks list, fallback_text string)
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📊 Special Days Statistics",
            },
        }
    ]

    # Overview section with fields
    feature_status = "✅ Enabled" if stats.get("feature_enabled") else "❌ Disabled"

    blocks.append(
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Total Special Days:*\n{stats.get('total_days', 0)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Currently Enabled:*\n{stats.get('enabled_days', 0)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Feature Status:*\n{feature_status}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Current Personality:*\n{stats.get('current_personality', 'N/A')}",
                },
            ],
        }
    )

    # Category breakdown
    by_category = stats.get("by_category", {})
    if by_category:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*By Category:*"},
            }
        )

        category_text = ""
        for category, cat_stats in by_category.items():
            cat_status = "✅" if cat_stats.get("category_enabled") else "❌"
            enabled_count = cat_stats.get("enabled", 0)
            total_count = cat_stats.get("total", 0)
            category_text += f"• {cat_status} *{category}:* {enabled_count}/{total_count} days\n"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": category_text}})

    # Source breakdown
    by_source = stats.get("by_source", {})
    if by_source:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*By Source:*"},
            }
        )

        source_text = ""
        for source, count in sorted(by_source.items(), key=lambda x: -x[1]):
            source_text += f"• *{source}:* {count}\n"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": source_text}})

    # Context footer
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "💡 Use `admin special` to manage special days | `admin special observances` for cache status",
                }
            ],
        }
    )

    # Fallback text
    fallback_text = f"Special Days Statistics: {stats.get('total_days', 0)} total, {stats.get('enabled_days', 0)} enabled"

    return blocks, fallback_text
