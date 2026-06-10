"""
Lazy official-description enrichment for special day observances.

Scraped observance records (UN/UNESCO/WHO) carry an official URL but an empty
description. On announcement day this module fetches the official page for
each of today's observances, extracts a faithful 1-2 sentence description
grounded strictly in the page text, and caches it permanently (keyed by URL).

Two entry points:
- apply_cached_descriptions(days): cache-only fill, no network — safe to call
  on every retrieval (slash commands, App Home, digests).
- enrich_descriptions(days): fetch + LLM extraction for cache misses — called
  only by the daily announcement job (1-5 pages/day).
"""

import json
import os
import threading
from html.parser import HTMLParser

import requests

from config import (
    OBSERVANCE_DESCRIPTION_FETCH_TIMEOUT,
    OBSERVANCE_DESCRIPTION_PAGE_MAX_CHARS,
    OBSERVANCE_DESCRIPTIONS_CACHE_FILE,
    REASONING_EFFORT,
    TEMPERATURE_SETTINGS,
    TOKEN_LIMITS,
    get_logger,
)

logger = get_logger("special_days")

_cache_lock = threading.Lock()
_cache_state: tuple | None = None  # (mtime_or_None, dict)


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style/nav/header/footer."""

    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            text = data.strip()
            if text:
                self.chunks.append(text)


def _load_cache() -> dict:
    """Load the descriptions cache (memoized by file mtime)."""
    global _cache_state

    try:
        mtime = os.path.getmtime(OBSERVANCE_DESCRIPTIONS_CACHE_FILE)
    except OSError:
        mtime = None

    # File read happens under the same lock _save_entry writes under, so a
    # concurrent save can't produce a torn read
    with _cache_lock:
        if _cache_state is not None and _cache_state[0] == mtime:
            return _cache_state[1]

        cache: dict = {}
        if mtime is not None:
            try:
                with open(OBSERVANCE_DESCRIPTIONS_CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"DESCRIPTION: Cache unreadable, starting fresh: {e}")
                cache = {}

        _cache_state = (mtime, cache)
        return cache


def _save_entry(url: str, name: str, description: str) -> None:
    """Persist one description to the cache file."""
    global _cache_state

    with _cache_lock:
        cache = dict(_cache_state[1]) if _cache_state else {}
        cache[url] = {"name": name, "description": description}
        try:
            with open(OBSERVANCE_DESCRIPTIONS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)
            mtime = os.path.getmtime(OBSERVANCE_DESCRIPTIONS_CACHE_FILE)
            _cache_state = (mtime, cache)
        except OSError as e:
            logger.error(f"DESCRIPTION: Failed to persist cache: {e}")


# Redirect/download bounds for official-page fetches
_MAX_REDIRECTS = 3
_PAGE_DOWNLOAD_MAX_BYTES = 512 * 1024


def _fetch_page_text(url: str) -> str:
    """
    Fetch an official observance page and return its visible text.

    Redirects are followed manually (max 3 hops) and every hop must stay on
    HTTPS — requests' automatic redirect following would silently bypass the
    scheme check. The download is capped to avoid parsing huge pages.
    """
    from urllib.parse import urljoin

    headers = {"User-Agent": "BrightDayBot/1.0 (+observance description fetch)"}
    response = None

    for _ in range(_MAX_REDIRECTS + 1):
        if not url.startswith("https://"):
            logger.warning(f"DESCRIPTION: Skipping non-HTTPS url: {url}")
            return ""

        response = requests.get(
            url,
            timeout=OBSERVANCE_DESCRIPTION_FETCH_TIMEOUT,
            headers=headers,
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location", "")
            url = urljoin(url, location)
            continue
        break
    else:
        logger.warning(f"DESCRIPTION: Too many redirects for {url}")
        return ""

    response.raise_for_status()

    # Cap the download; iter_content handles content-encoding decompression
    chunks = []
    size = 0
    for chunk in response.iter_content(chunk_size=16384):
        chunks.append(chunk)
        size += len(chunk)
        if size >= _PAGE_DOWNLOAD_MAX_BYTES:
            break
    html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")

    parser = _TextExtractor()
    parser.feed(html)
    text = " ".join(parser.chunks)
    return text[:OBSERVANCE_DESCRIPTION_PAGE_MAX_CHARS]


def get_official_description(name: str, url: str) -> str:
    """
    Return the official description for an observance ('' on failure).

    Cache hit: returned immediately. Cache miss: fetch the official page and
    extract a description grounded strictly in the page text. Only successes
    are cached, so transient failures retry on the next announcement day.
    """
    if not url:
        return ""

    cached = _load_cache().get(url)
    if cached is not None:
        return cached.get("description", "")

    description = ""
    try:
        page_text = _fetch_page_text(url)
        if page_text:
            # Local import so module import never requires an OpenAI key
            from integrations.openai import complete

            description = (
                complete(
                    input_text=(
                        f'Below is text from the official page for "{name}":\n\n'
                        f"{page_text}\n\n"
                        f'Extract a faithful 1-2 sentence description of "{name}" '
                        "using ONLY information present in the text above. Closely "
                        "paraphrase or quote the source — no embellishment, no "
                        "invented facts, no dates that are not in the text. "
                        "Return only the description, nothing else."
                    ),
                    max_tokens=TOKEN_LIMITS.get("observance_description", 800),
                    temperature=TEMPERATURE_SETTINGS.get("factual", 0.3),
                    reasoning_effort=REASONING_EFFORT["analytical"],
                    context="OBSERVANCE_DESCRIPTION",
                )
                or ""
            ).strip()
    except Exception as e:
        logger.warning(f"DESCRIPTION: Could not fetch/extract for '{name}': {e}")

    if description:
        _save_entry(url, name, description)
        logger.info(f"DESCRIPTION: Cached official description for '{name}'")

    return description


def apply_cached_descriptions(special_days) -> None:
    """Fill empty descriptions in-place from cache only (no network calls)."""
    cache = _load_cache()
    if not cache:
        return

    for day in special_days:
        if not getattr(day, "description", "") and getattr(day, "url", ""):
            cached = cache.get(day.url)
            if cached and cached.get("description"):
                day.description = cached["description"]


def enrich_descriptions(special_days) -> None:
    """
    Fill empty descriptions in-place, fetching official pages on cache miss.

    Network + one LLM call per uncached observance — intended for the daily
    announcement job only (a handful of days at most, cached permanently).
    """
    for day in special_days:
        try:
            if not getattr(day, "description", "") and getattr(day, "url", ""):
                description = get_official_description(day.name, day.url)
                if description:
                    day.description = description
        except Exception as e:
            logger.warning(f"DESCRIPTION: Enrichment failed for '{getattr(day, 'name', '?')}': {e}")
