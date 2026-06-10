"""
Calendarific API Client for BrightDayBot

Multi-source holiday fetcher with per-source caching and shared rate limiting.
Each source represents a country/region with its own category, emoji, and
optional holiday name whitelist. All sources fetch yearly: one API call
caches the entire year per source.

Sources are configured in config/settings.py CALENDARIFIC_SOURCES.
API docs: https://calendarific.com/api-documentation
"""

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

from config import (
    CACHE_RETENTION_DAYS,
    CALENDARIFIC_API_KEY,
    CALENDARIFIC_CACHE_DIR,
    CALENDARIFIC_CACHE_TTL_DAYS,
    CALENDARIFIC_ENABLED,
    CALENDARIFIC_RATE_LIMIT_MONTHLY,
    CALENDARIFIC_RATE_WARNING_THRESHOLD,
    CALENDARIFIC_SOURCES,
    CALENDARIFIC_SOURCES_STATE_FILE,
    CALENDARIFIC_STATS_FILE,
    HEALTH_CATEGORY_KEYWORDS,
    TECH_CATEGORY_KEYWORDS,
    TIMEOUTS,
    get_logger,
)

logger = get_logger("calendarific")

_client: Optional["CalendarificClient"] = None
_client_lock = threading.Lock()


def get_calendarific_client() -> "CalendarificClient":
    """Get or create the singleton Calendarific client (thread-safe)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = CalendarificClient()
    return _client


@dataclass
class CalendarificSource:
    """A configured Calendarific holiday source (country/region)."""

    id: str
    country: str
    state: Optional[str] = None
    enabled: bool = True
    label: str = ""
    category: str = "Holiday"
    emoji: str = "📅"
    whitelist: List[str] = field(default_factory=list)
    api_type: str = "national,local"

    @classmethod
    def from_dict(cls, d: dict) -> "CalendarificSource":
        return cls(
            id=d["id"],
            country=d["country"],
            state=d.get("state"),
            enabled=d.get("enabled", True),
            label=d.get("label", d["country"]),
            category=d.get("category", "Holiday"),
            emoji=d.get("emoji", "📅"),
            whitelist=d.get("whitelist", []),
            api_type=d.get("api_type", "national,local"),
        )

    @property
    def cache_file(self) -> str:
        return os.path.join(CALENDARIFIC_CACHE_DIR, f"{self.id}_cache.json")

    @property
    def source_label(self) -> str:
        return f"Calendarific ({self.country})"


class CalendarificClient:
    """Multi-source Calendarific API client with per-source caching."""

    BASE_URL = "https://calendarific.com/api/v2/holidays"

    def __init__(self):
        self.api_key = CALENDARIFIC_API_KEY
        self.cache_dir = CALENDARIFIC_CACHE_DIR
        self.cache_ttl_days = CALENDARIFIC_CACHE_TTL_DAYS
        self.sources = [CalendarificSource.from_dict(s) for s in CALENDARIFIC_SOURCES]

        # Validate unique source IDs
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            dupes = [sid for sid in ids if ids.count(sid) > 1]
            logger.error(f"CALENDARIFIC: Duplicate source IDs detected: {set(dupes)}")

        # Apply persisted enabled/disabled state from file
        self._apply_saved_state()

        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

    def _apply_saved_state(self):
        """Override source enabled flags from persisted state file."""
        try:
            if os.path.exists(CALENDARIFIC_SOURCES_STATE_FILE):
                with open(CALENDARIFIC_SOURCES_STATE_FILE, "r") as f:
                    saved = json.load(f)
                for src in self.sources:
                    if src.id in saved:
                        src.enabled = saved[src.id]
        except (json.JSONDecodeError, OSError):
            pass

    def save_source_state(self):
        """Persist current enabled/disabled state to file."""
        state = {src.id: src.enabled for src in self.sources}
        try:
            os.makedirs(os.path.dirname(CALENDARIFIC_SOURCES_STATE_FILE), exist_ok=True)
            with open(CALENDARIFIC_SOURCES_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass

    def get_enabled_sources(self) -> List[CalendarificSource]:
        return [s for s in self.sources if s.enabled]

    # ---- Per-date holiday access ----

    def get_holidays_for_date(self, date: datetime) -> List["SpecialDay"]:
        """Get holidays from all enabled sources for a specific date."""
        results = []
        for source in self.get_enabled_sources():
            results.extend(self._get_source_holidays_for_date(source, date))
        return results

    def _get_source_holidays_for_date(
        self, source: CalendarificSource, date: datetime
    ) -> List["SpecialDay"]:
        cache_data = self._load_cache(source)
        date_key = date.strftime("%Y-%m-%d")

        # All sources use the yearly strategy: one API call caches the whole
        # year, refreshed when the cached year doesn't match the request
        cached_year = cache_data.get("year")
        if not cache_data.get("cached_at") or cached_year != date.year:
            logger.info(f"CALENDARIFIC [{source.id}]: Auto-populating yearly cache...")
            self._prefetch_yearly(source, force=True)
            cache_data = self._load_cache(source)

        entry = cache_data.get("entries", {}).get(date_key)
        if not entry:
            return []
        return [
            self._dict_to_special_day(h, source)
            for h in entry.get("holidays", [])
            if self._matches_source_filter(h, source)
        ]

    # ---- Prefetching ----

    def prefetch_all(self, force: bool = False) -> Dict[str, dict]:
        """Prefetch all enabled sources (yearly). Returns per-source stats."""
        results = {}
        for source in self.get_enabled_sources():
            results[source.id] = self._prefetch_yearly(source, force=force)
        self._update_last_prefetch()
        return results

    # Backward compatibility
    def weekly_prefetch(self, days_ahead: int = None, force: bool = False) -> Dict:
        return self.prefetch_all(force=force)

    def _prefetch_yearly(self, source: CalendarificSource, force: bool = False) -> Dict:
        if not self.api_key:
            return {"error": "No API key"}

        if not force:
            cache_data = self._load_cache(source)
            # Yearly sources: fresh if cached for the current year
            cached_year = cache_data.get("year")
            if cached_year == datetime.now().year and cache_data.get("cached_at"):
                return {"skipped": "Cache fresh (current year)"}

        try:
            self._check_rate_limit()
            year = datetime.now().year
            holidays = self._fetch_from_api(source, year)
            self._increment_rate_counter()

            if source.whitelist:
                holidays = [h for h in holidays if self._matches_source_filter(h, source)]

            # Assign emojis via LLM (with keyword fallback)
            holidays = self._enrich_holidays_with_emojis(holidays)

            cache_data = {"entries": {}, "cached_at": datetime.now().isoformat(), "year": year}
            for h in holidays:
                date_info = h.get("date", {})
                iso = date_info.get("iso", "").split("T")[0] if isinstance(date_info, dict) else ""
                if iso:
                    cache_data["entries"].setdefault(iso, {"holidays": []})["holidays"].append(h)

            self._save_cache(source, cache_data)
            logger.info(
                f"CALENDARIFIC [{source.id}]: Yearly prefetch — "
                f"{len(holidays)} holidays across {len(cache_data['entries'])} dates"
            )
            return {"fetched": len(holidays), "dates": len(cache_data["entries"]), "api_calls": 1}

        except Exception as e:
            logger.warning(f"CALENDARIFIC [{source.id}]: Yearly prefetch failed: {e}")
            return {"error": str(e)}

    # ---- API ----

    def _fetch_from_api(
        self, source: CalendarificSource, year: int, month: int = None, day: int = None
    ) -> List[Dict]:
        params = {
            "api_key": self.api_key,
            "country": source.country,
            "year": year,
        }
        if source.api_type:
            params["type"] = source.api_type
        if month is not None:
            params["month"] = month
        if day is not None:
            params["day"] = day

        resp = requests.get(self.BASE_URL, params=params, timeout=TIMEOUTS.get("http_request", 30))
        resp.raise_for_status()
        data = resp.json()

        if data.get("meta", {}).get("code") != 200:
            raise requests.RequestException(
                f"API error: {data.get('meta', {}).get('error_detail', 'Unknown')}"
            )

        body = data.get("response", {})
        holidays = body.get("holidays", []) if isinstance(body, dict) else []

        # Whitelisted sources: keep all holidays (whitelist filter applied later)
        # Non-whitelisted: filter to national/local/observance types only
        if source.whitelist:
            return holidays

        return [
            h
            for h in holidays
            if any(
                kw in t
                for t in [t.lower() for t in h.get("type", [])]
                for kw in ["national", "local", "observance"]
            )
        ]

    # ---- Filtering & Conversion ----

    def _matches_source_filter(self, holiday: Dict, source: CalendarificSource) -> bool:
        """Check if a holiday passes the source's whitelist/exclusion filter."""
        name = holiday.get("name", "").lower()
        if not source.whitelist:
            return True
        import re

        # Whitelist: name must match a whitelist term at word boundary
        wl = [w.lower() for w in source.whitelist]
        if not any(re.search(rf"\b{re.escape(w)}\b", name) for w in wl):
            return False
        # Exclude extension/variant days
        if any(kw in name for kw in ["holiday", "day off", "observed", "substitute"]):
            return False
        day_match = re.search(r"\(day (\d+)\)", name)
        if day_match and int(day_match.group(1)) > 1:
            return False
        if name.endswith(" eve"):
            return False
        # Exclude parenthetical variants like (Smarta), (substitute)
        # but keep descriptive ones like (Muslim New Year), (Day 1)
        paren = re.search(r"\(([^)]+)\)$", name)
        if paren:
            inner = paren.group(1).lower()
            if inner not in ("day 1",) and not any(kw in inner for kw in ["new year", "day 1"]):
                return False
        return True

    def _dict_to_special_day(self, holiday: Dict, source: CalendarificSource) -> "SpecialDay":
        from storage.special_days import SpecialDay

        name = holiday.get("name", "Unknown Observance")
        description = holiday.get("description", "")

        date_str = ""
        date_info = holiday.get("date", {})
        if isinstance(date_info, dict) and date_info.get("iso"):
            try:
                dt = datetime.fromisoformat(date_info["iso"].split("T")[0])
                date_str = dt.strftime("%d/%m")
            except ValueError:
                pass

        # Use LLM-assigned emoji if available, then source default, then keyword fallback
        emoji = holiday.get("_emoji") or (
            source.emoji if source.whitelist else self._select_emoji(name)
        )
        category = (
            source.category
            if source.whitelist
            else self._map_type_to_category(holiday, default=source.category)
        )

        return SpecialDay(
            date=date_str,
            name=name,
            category=category,
            description=description or f"{source.label}: {name}",
            emoji=emoji,
            enabled=True,
            source=source.source_label,
            url="",
        )

    def _map_type_to_category(self, holiday: Dict, default: str = "Culture") -> str:
        combined = f"{holiday.get('name', '')} {holiday.get('description', '')}".lower()
        if any(t in combined for t in HEALTH_CATEGORY_KEYWORDS):
            return "Global Health"
        if any(t in combined for t in TECH_CATEGORY_KEYWORDS):
            return "Tech"
        return default

    def _select_emoji(self, name: str) -> str:
        n = name.lower()
        if "eid" in n:
            return "🌙"
        if "ramadan" in n:
            return "🕌"
        if "christmas" in n:
            return "🎄"
        if "easter" in n:
            return "🐣"
        if "new year" in n:
            return "🎆"
        return "📅"

    def _assign_emojis_via_llm(self, holidays: List[Dict]) -> Dict[str, str]:
        """Use LLM to assign emojis for holiday names. Returns {name: emoji} mapping.

        Retries once on parse failure before falling back to keyword matching.
        """
        names = list({h.get("name", "") for h in holidays if h.get("name")})
        if not names:
            return {}

        from integrations.openai import complete

        prompt = (
            "Assign exactly one emoji to each holiday/observance below. "
            "Pick the most representative emoji for each.\n\n"
            + "\n".join(f"- {name}" for name in names)
        )
        instructions = (
            "Return ONLY a JSON object mapping each holiday name to one emoji. "
            'Example: {"Christmas Day": "🎄", "New Year": "🎆"}. '
            "No explanation, no markdown, just the JSON object."
        )

        from config import LLM_PARSE_MAX_RETRIES

        for attempt in range(LLM_PARSE_MAX_RETRIES):
            try:
                response = complete(
                    input_text=prompt,
                    instructions=instructions,
                    max_tokens=len(names) * 20,
                    temperature=0.3,
                    context="EMOJI_ASSIGNMENT",
                )

                if response:
                    import json as _json

                    text = response.strip()
                    if text.startswith("```"):
                        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    mapping = _json.loads(text)
                    if isinstance(mapping, dict):
                        logger.info(
                            f"CALENDARIFIC: LLM assigned emojis for {len(mapping)} holidays"
                        )
                        return mapping
                    logger.debug(f"CALENDARIFIC: LLM returned non-dict: {type(mapping)}")
                else:
                    logger.debug("CALENDARIFIC: LLM returned empty response")
            except (ValueError, KeyError) as e:
                logger.debug(
                    f"CALENDARIFIC: Emoji parse failed (attempt {attempt + 1}/{LLM_PARSE_MAX_RETRIES}): {e}"
                )
            except Exception as e:
                logger.debug(f"CALENDARIFIC: Emoji LLM call failed: {e}")
                break  # Don't retry on API/network errors

        return {}

    def _enrich_holidays_with_emojis(self, holidays: List[Dict]) -> List[Dict]:
        """Add '_emoji' field to holidays via LLM, falling back to keyword matching."""
        llm_emojis = self._assign_emojis_via_llm(holidays)

        for h in holidays:
            name = h.get("name", "")
            if name in llm_emojis:
                h["_emoji"] = llm_emojis[name]
            else:
                h["_emoji"] = self._select_emoji(name)
        return holidays

    # ---- Cache I/O ----

    def _load_cache(self, source: CalendarificSource) -> Dict:
        if not os.path.exists(source.cache_file):
            return {"entries": {}}
        try:
            with open(source.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if "entries" in data else {"entries": {}}
        except (json.JSONDecodeError, OSError):
            return {"entries": {}}

    def _save_cache(self, source: CalendarificSource, cache_data: Dict):
        cache_data["last_saved"] = datetime.now().isoformat()
        try:
            os.makedirs(os.path.dirname(source.cache_file), exist_ok=True)
            tmp = source.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, source.cache_file)
        except OSError as e:
            logger.warning(f"CALENDARIFIC [{source.id}]: Failed to save cache: {e}")

    def _is_source_cache_fresh(self, source: CalendarificSource, cache_data: Dict = None) -> bool:
        """Check if a source's cache is within TTL."""
        if cache_data is None:
            cache_data = self._load_cache(source)
        ts = cache_data.get("last_saved") or cache_data.get("cached_at")
        if not ts:
            return bool(cache_data.get("entries"))  # Has data but no timestamp
        try:
            age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 86400
            return age < self.cache_ttl_days
        except (ValueError, TypeError):
            return False

    # ---- Rate limiting ----

    def _load_stats(self) -> Dict:
        if not os.path.exists(CALENDARIFIC_STATS_FILE):
            return {"monthly_calls": {}, "last_prefetch": None}
        try:
            with open(CALENDARIFIC_STATS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"monthly_calls": {}, "last_prefetch": None}

    def _save_stats(self, stats: Dict):
        stats["last_saved"] = datetime.now().isoformat()
        try:
            os.makedirs(os.path.dirname(CALENDARIFIC_STATS_FILE), exist_ok=True)
            with open(CALENDARIFIC_STATS_FILE, "w") as f:
                json.dump(stats, f, indent=2, sort_keys=True)
        except OSError:
            pass

    def _get_rate_count(self) -> int:
        return self._load_stats().get("monthly_calls", {}).get(datetime.now().strftime("%Y-%m"), 0)

    def _increment_rate_counter(self):
        stats = self._load_stats()
        month = datetime.now().strftime("%Y-%m")
        stats.setdefault("monthly_calls", {})[month] = (
            stats.get("monthly_calls", {}).get(month, 0) + 1
        )
        self._save_stats(stats)

    def _check_rate_limit(self):
        count = self._get_rate_count()
        if count >= CALENDARIFIC_RATE_LIMIT_MONTHLY:
            raise RateLimitExceeded(f"Monthly limit of {CALENDARIFIC_RATE_LIMIT_MONTHLY} reached")
        if count >= CALENDARIFIC_RATE_WARNING_THRESHOLD:
            logger.warning(
                f"CALENDARIFIC: {count}/{CALENDARIFIC_RATE_LIMIT_MONTHLY} API calls this month"
            )

    def _update_last_prefetch(self):
        stats = self._load_stats()
        stats["last_prefetch"] = datetime.now().isoformat()
        self._save_stats(stats)

    def get_last_prefetch(self) -> Optional[datetime]:
        lp = self._load_stats().get("last_prefetch")
        if not lp:
            return None
        try:
            return datetime.fromisoformat(lp)
        except (ValueError, TypeError):
            return None

    def needs_prefetch(self) -> bool:
        last = self.get_last_prefetch()
        return last is None or (datetime.now() - last).days >= self.cache_ttl_days

    # ---- Aggregation ----

    def get_all_cached_special_days(self) -> list:
        """All cached SpecialDay objects from all enabled sources."""
        days = []
        for source in self.get_enabled_sources():
            try:
                cache_data = self._load_cache(source)
                for entry in cache_data.get("entries", {}).values():
                    for h in entry.get("holidays", []):
                        if self._matches_source_filter(h, source):
                            sd = self._dict_to_special_day(h, source)
                            if sd.date:
                                days.append(sd)
            except Exception as e:
                logger.debug(f"CALENDARIFIC [{source.id}]: Cache load failed: {e}")
        return days

    def get_cached_holiday_count(self, source: CalendarificSource = None) -> int:
        """Unique holiday count by (DD/MM, name). If source is None, count all."""
        sources = [source] if source else self.get_enabled_sources()
        seen = set()
        for src in sources:
            for entry in self._load_cache(src).get("entries", {}).values():
                for h in entry.get("holidays", []):
                    if not self._matches_source_filter(h, src):
                        continue
                    date_info = h.get("date", {})
                    if isinstance(date_info, dict) and date_info.get("iso"):
                        try:
                            dt = datetime.fromisoformat(date_info["iso"].split("T")[0])
                            seen.add((dt.strftime("%d/%m"), h.get("name", "").lower().strip()))
                        except (ValueError, KeyError):
                            continue
        return len(seen)

    def get_api_status(self) -> Dict:
        """Aggregated API status across all sources."""
        month_calls = self._get_rate_count()
        last_prefetch = self.get_last_prefetch()

        per_source = {}
        for src in self.sources:
            cache_data = self._load_cache(src) if src.enabled else {}
            last_saved = cache_data.get("last_saved") or cache_data.get("cached_at")
            per_source[src.id] = {
                "label": src.label,
                "country": src.country,
                "enabled": src.enabled,
                "holiday_count": self.get_cached_holiday_count(src) if src.enabled else 0,
                "last_updated": last_saved,
                "cache_fresh": (
                    self._is_source_cache_fresh(src, cache_data) if src.enabled else False
                ),
            }

        return {
            "enabled": CALENDARIFIC_ENABLED,
            "api_key_configured": bool(self.api_key),
            "sources": per_source,
            "month_calls": month_calls,
            "monthly_limit": CALENDARIFIC_RATE_LIMIT_MONTHLY,
            "calls_remaining": CALENDARIFIC_RATE_LIMIT_MONTHLY - month_calls,
            "holiday_count": self.get_cached_holiday_count(),
            "cache_ttl_days": self.cache_ttl_days,
            "last_prefetch": last_prefetch.isoformat() if last_prefetch else None,
            "needs_prefetch": self.needs_prefetch(),
        }

    def clear_cache(self, source_id: str = None):
        targets = self.sources if not source_id else [s for s in self.sources if s.id == source_id]
        for src in targets:
            if os.path.exists(src.cache_file):
                os.remove(src.cache_file)
                logger.info(f"CALENDARIFIC [{src.id}]: Cache cleared")

    def cleanup_old_cache(self, max_age_days: int = None):
        if max_age_days is None:
            max_age_days = CACHE_RETENTION_DAYS.get("calendarific", 30)
        cutoff = datetime.now() - timedelta(days=max_age_days)

        for source in self.sources:
            cache_data = self._load_cache(source)
            entries = cache_data.get("entries", {})
            to_remove = [
                dk
                for dk, entry in entries.items()
                if entry.get("cached_at") and datetime.fromisoformat(entry["cached_at"]) < cutoff
            ]
            if to_remove:
                for dk in to_remove:
                    del entries[dk]
                self._save_cache(source, cache_data)
                logger.info(f"CALENDARIFIC [{source.id}]: Removed {len(to_remove)} old entries")


class RateLimitExceeded(Exception):
    pass
