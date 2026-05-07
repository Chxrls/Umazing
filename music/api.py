"""
music/api.py — All HTTP calls to the umapyoi.net Music API.

Base URL: https://umapyoi.net/api/v1/music/
Rate limits (as documented): 10 req/s · 500 req/min · 7 200 req/hr

Confirmed field names (verified from live API responses):
  Album:   id, name_en, name_jp, album_art, slug, release_date
  Track:   id, name_en, name_jp, preview_url, runtime, slug, _singers, disc_no, disc_order
  Singer:  id, chara_name_en, chara_name_jp, chara_image, preferred_url, va_name_en
  Filter:  {"albums": [...], "tracks": [...]}
  Filters: {"album": [...], "character": [...], "song": [...], "voiceactor": [...]}
           Each filter entry: {"id": "<slug>", "display": "<human name>"}
"""
from __future__ import annotations

import asyncio
import logging
import re
from difflib import SequenceMatcher
from typing import Any

import aiohttp

log = logging.getLogger("umapyoi.music.api")

MUSIC_BASE = "https://umapyoi.net/api/v1/music"

# ─── session reference (injected by bot.py on_ready) ───────────────────────

_session: aiohttp.ClientSession | None = None

# ─── filter cache (populated lazily) ──────────────────────────────────────
# Structure: {"song": [{"id": slug, "display": name}, ...], "character": [...], "album": [...]}
_filter_cache: dict[str, list[dict]] | None = None


def set_session(s: aiohttp.ClientSession) -> None:
    global _session
    _session = s


# ─── low-level request helper ──────────────────────────────────────────────

async def _get(url: str, params: dict | None = None) -> tuple[int, Any]:
    """Return (status_code, parsed_json_or_None)."""
    assert _session is not None, "HTTP session not initialised"
    log.debug("GET %s params=%s", url, params)
    try:
        async with _session.get(url, params=params) as resp:
            status = resp.status
            if status == 204:
                return 204, None
            if status != 200:
                log.warning("Non-200 from %s: %d", url, status)
                return status, None
            return 200, await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        log.exception("HTTP error fetching %s: %s", url, exc)
        return 0, None


# ─── slug helper ───────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert user input to a basic slug (fallback only).
    Prefer resolve_song_slug() / resolve_character_slug() which hit the live filter list.
    """
    text = text.lower().strip()
    text = re.sub(r"[\s_/]+", "-", text)
    text = re.sub(r"[^a-z0-9\-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


# ─── filter-aware slug resolvers ───────────────────────────────────────────

async def _ensure_filter_cache() -> dict[str, list[dict]]:
    """Lazily load and cache the /filters endpoint."""
    global _filter_cache
    if _filter_cache is not None:
        return _filter_cache
    status, data = await _get(f"{MUSIC_BASE}/filters")
    if status == 200 and data:
        _filter_cache = data
    else:
        _filter_cache = {"song": [], "character": [], "album": []}
    return _filter_cache


def _best_slug_match(query: str, entries: list[dict]) -> str | None:
    """
    Find the best matching slug from a list of {"id": slug, "display": name} entries.
    Tries: exact id match → exact display match → substring → fuzzy ratio.
    Returns the slug (id field) of the best match, or None.
    """
    q_lower = query.lower().strip()

    # 1. Exact slug match
    for e in entries:
        if e["id"] == q_lower:
            return e["id"]

    # 2. Exact display name match (case-insensitive)
    for e in entries:
        if e["display"].lower() == q_lower:
            return e["id"]

    # 3. Substring match on display
    matches = [e for e in entries if q_lower in e["display"].lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        # Pick closest by length
        matches.sort(key=lambda e: abs(len(e["display"]) - len(query)))
        return matches[0]["id"]

    # 4. Fuzzy ratio fallback
    best, best_score = None, 0.0
    for e in entries:
        score = SequenceMatcher(None, q_lower, e["display"].lower()).ratio()
        if score > best_score:
            best_score = score
            best = e["id"]
    return best if best_score >= 0.45 else None


async def resolve_song_slug(query: str) -> str | None:
    """Return the official API slug for a song name, or None if no match."""
    cache = await _ensure_filter_cache()
    return _best_slug_match(query, cache.get("song", []))


async def resolve_character_slug(query: str) -> str | None:
    """Return the official API slug for a character name, or None if no match."""
    cache = await _ensure_filter_cache()
    return _best_slug_match(query, cache.get("character", []))


async def resolve_album_slug(query: str) -> str | None:
    """Return the official API slug for an album name, or None if no match."""
    cache = await _ensure_filter_cache()
    return _best_slug_match(query, cache.get("album", []))


async def search_songs(query: str) -> list[dict]:
    """Return all song filter entries whose display name contains the query."""
    cache = await _ensure_filter_cache()
    q = query.lower()
    return [e for e in cache.get("song", []) if q in e["display"].lower()]


# ─── Album endpoints ───────────────────────────────────────────────────────

async def fetch_albums_page(page: int) -> tuple[int, list[dict] | None]:
    """GET /music/min/albums/<page>  — 10 albums per page, page 0 = first."""
    return await _get(f"{MUSIC_BASE}/min/albums/{page}")


async def fetch_all_albums() -> list[dict]:
    """Iterate all paginated pages and collect every album.
    Stops when an empty page (or non-200) is returned.
    """
    albums: list[dict] = []
    page = 0
    while True:
        status, data = await fetch_albums_page(page)
        if status != 200 or not data:
            break
        albums.extend(data)
        if len(data) < 10:
            # Last page was partial → no more pages
            break
        page += 1
        await asyncio.sleep(0.12)  # ~8 req/s to stay under the 10 req/s limit
    return albums


async def fetch_album(album_id: int) -> tuple[int, dict | None]:
    """GET /music/album/<album_id>  — full album including _tracks."""
    return await _get(f"{MUSIC_BASE}/album/{album_id}")


# ─── Filter endpoints ──────────────────────────────────────────────────────

async def fetch_filters() -> tuple[int, dict | None]:
    """GET /music/filters  — lists of valid filter slugs by category."""
    return await _get(f"{MUSIC_BASE}/filters")


async def fetch_filter(
    *,
    song: str | None = None,
    character: str | None = None,
    album: str | None = None,
    search: str | None = None,
) -> tuple[int, dict | None]:
    """GET /music/filter  — returns {"albums": [...], "tracks": [...]}.

    All parameters must already be slugified before calling this function.
    Multiple values for the same key can be separated by '~' in the slug.
    """
    params: dict[str, str] = {}
    if song:
        params["song"] = song
    if character:
        params["character"] = character
    if album:
        params["album"] = album
    if search:
        params["search"] = search
    return await _get(f"{MUSIC_BASE}/filter", params=params)


# ─── Convenience helpers ───────────────────────────────────────────────────

def get_album_art(album: dict) -> str | None:
    """Return the album art URL if present."""
    return album.get("album_art")


def get_track_preview(track: dict) -> str | None:
    """Return the Apple Music preview URL for a track, or None."""
    return track.get("preview_url")


def track_singers_str(track: dict) -> str:
    """Return a comma-separated English name string of all singers."""
    singers = track.get("_singers", [])
    names = [s.get("chara_name_en", "Unknown") for s in singers if s.get("chara_name_en")]
    return ", ".join(names) if names else "Unknown"


def track_album_name(track: dict) -> str:
    """Return the name of the first album this track appears in (from filter result)."""
    albums = track.get("_albums", [])
    if albums:
        return albums[0].get("name_en", "Unknown Album")
    return "Unknown Album"


def track_album_art(track: dict) -> str | None:
    """Return album art from the first _albums entry in a filter result track."""
    albums = track.get("_albums", [])
    if albums:
        return albums[0].get("album_art")
    return None


def fuzzy_match_album(albums: list[dict], query: str) -> dict | None:
    """Case-insensitive substring match on name_en."""
    q = query.lower()
    # Exact match first
    for a in albums:
        if a.get("name_en", "").lower() == q:
            return a
    # Substring match
    for a in albums:
        if q in a.get("name_en", "").lower():
            return a
    return None
