"""
music/player.py — VoiceClient manager, queue, playback, auto-disconnect.

Architecture:
  - GuildPlayer tracks one guild's VC connection, queue, and playback state.
  - MusicManager is a module-level dict keyed by guild_id holding GuildPlayers.
  - Audio resolution uses yt-dlp via asyncio.create_subprocess_exec (non-blocking).
  - Preview URLs from the API are used only as a labelled fallback.
"""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
from dataclasses import dataclass, field
from typing import Optional

import discord

log = logging.getLogger("umapyoi.music.player")

# ─── Resolve binary paths at import time ─────────────────────────────────────
# shutil.which() locates the executable on the system PATH.
# On Railway/Linux: ffmpeg and yt-dlp are installed via nixpacks.toml.
# On Windows (local dev): installed via winget.
# Falls back to bare command name so discord.py gives a clear error if missing.

FFMPEG_EXE: str = shutil.which("ffmpeg") or "ffmpeg"
YTDLP_EXE:  str = shutil.which("yt-dlp") or "yt-dlp"

log.info("ffmpeg  → %s", FFMPEG_EXE)
log.info("yt-dlp  → %s", YTDLP_EXE)

# yt-dlp CLI options
YTDLP_OPTS = [
    "--no-playlist",
    "--format", "bestaudio/best",
    "--get-url",
    "--no-warnings",
    "--quiet",
]

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

INACTIVITY_TIMEOUT = 300  # 5 minutes


@dataclass
class TrackInfo:
    """Metadata for one track in the queue."""
    track_id: int
    name_en: str
    album_name: str
    album_art: str | None          # URL
    preview_url: str | None        # Apple Music preview (short clip)
    singers: str                   # comma-separated character names
    runtime: int | None            # seconds (may be None)
    # Resolved at playback time:
    stream_url: str | None = None
    is_preview: bool = False       # True if we're playing preview instead of full


class GuildPlayer:
    """Manages playback state for a single guild."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.voice_client: Optional[discord.VoiceClient] = None
        self.queue: list[TrackInfo] = []
        self.current: Optional[TrackInfo] = None
        self.loop = False
        self.shuffle = False
        self._inactivity_task: Optional[asyncio.Task] = None
        self._started_at: float = 0.0   # monotonic time when current track started

    # ── connection ───────────────────────────────────────────────────────────

    async def connect(self, channel: discord.VoiceChannel) -> None:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel != channel:
                await self.voice_client.move_to(channel)
        else:
            self.voice_client = await channel.connect()

    async def disconnect(self) -> None:
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            self.voice_client = None
        self.queue.clear()
        self.current = None
        self._cancel_inactivity()

    # ── playback ─────────────────────────────────────────────────────────────

    async def play_track(self, track: TrackInfo, bot_loop: asyncio.AbstractEventLoop) -> None:
        """Resolve audio URL and begin streaming. Blocks briefly for yt-dlp."""
        self.current = track
        self._cancel_inactivity()

        stream_url, is_preview = await _resolve_audio(track, bot_loop)
        if not stream_url:
            log.warning("Could not resolve audio for '%s', skipping.", track.name_en)
            self._advance(bot_loop)
            return

        track.stream_url = stream_url
        track.is_preview = is_preview

        import time
        self._started_at = time.monotonic()

        source = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_EXE, **FFMPEG_OPTS)
        source = discord.PCMVolumeTransformer(source, volume=0.8)

        def after_play(err):
            if err:
                log.warning("Playback error for '%s': %s", track.name_en, err)
            bot_loop.call_soon_threadsafe(self._advance, bot_loop)

        if self.voice_client and self.voice_client.is_connected():
            self.voice_client.play(source, after=after_play)
        else:
            log.warning("VC disconnected before play for '%s'", track.name_en)

    def _advance(self, bot_loop: asyncio.AbstractEventLoop) -> None:
        """Called (thread-safe) after a track ends to play the next one."""
        if self.loop and self.current:
            asyncio.run_coroutine_threadsafe(
                self.play_track(self.current, bot_loop), bot_loop
            )
            return

        if not self.queue:
            self.current = None
            self._schedule_inactivity(bot_loop)
            return

        if self.shuffle:
            idx = random.randrange(len(self.queue))
            next_track = self.queue.pop(idx)
        else:
            next_track = self.queue.pop(0)

        asyncio.run_coroutine_threadsafe(
            self.play_track(next_track, bot_loop), bot_loop
        )

    # ── controls ─────────────────────────────────────────────────────────────

    def skip(self) -> None:
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    def pause(self) -> None:
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()

    def resume(self) -> None:
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()

    def stop(self) -> None:
        self.queue.clear()
        if self.voice_client and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        ):
            self.voice_client.stop()
        self.current = None

    def toggle_loop(self) -> bool:
        self.loop = not self.loop
        return self.loop

    def toggle_shuffle(self) -> bool:
        self.shuffle = not self.shuffle
        return self.shuffle

    def is_playing(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_playing())

    def is_paused(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_paused())

    def elapsed_seconds(self) -> int:
        """Best-effort elapsed time for the current track."""
        if not self.current or not self._started_at:
            return 0
        import time
        return int(time.monotonic() - self._started_at)

    # ── auto-disconnect ───────────────────────────────────────────────────────

    def _schedule_inactivity(self, bot_loop: asyncio.AbstractEventLoop) -> None:
        self._cancel_inactivity()
        self._inactivity_task = bot_loop.create_task(self._inactivity_check())

    def _cancel_inactivity(self) -> None:
        if self._inactivity_task and not self._inactivity_task.done():
            self._inactivity_task.cancel()
            self._inactivity_task = None

    async def _inactivity_check(self) -> None:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        if not self.is_playing() and not self.is_paused():
            log.info("Guild %d: auto-disconnecting after inactivity.", self.guild_id)
            await self.disconnect()


# ─── module-level manager ─────────────────────────────────────────────────

_players: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    """Return (or create) a GuildPlayer for the given guild."""
    if guild_id not in _players:
        _players[guild_id] = GuildPlayer(guild_id)
    return _players[guild_id]


def remove_player(guild_id: int) -> None:
    _players.pop(guild_id, None)


# ─── yt-dlp audio resolution ─────────────────────────────────────────────

async def _resolve_via_ytdlp(query: str, bot_loop: asyncio.AbstractEventLoop) -> str | None:
    """Run yt-dlp as a subprocess to resolve the best audio stream URL."""
    cmd = [
        YTDLP_EXE,
        f"ytsearch1:{query}",
        *YTDLP_OPTS,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            log.warning("yt-dlp returned %d: %s", proc.returncode, stderr.decode()[:200])
            return None
        url = stdout.decode().strip().split("\n")[0]
        return url if url.startswith("http") else None
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        log.warning("yt-dlp error: %s", exc)
        return None


async def _resolve_audio(
    track: TrackInfo, bot_loop: asyncio.AbstractEventLoop
) -> tuple[str | None, bool]:
    """
    Try yt-dlp first. Fall back to the API preview URL.
    Returns (url, is_preview).
    """
    yt_query = f"{track.name_en} Umamusume"
    url = await _resolve_via_ytdlp(yt_query, bot_loop)
    if url:
        return url, False

    if track.preview_url:
        log.info("Falling back to preview URL for '%s'.", track.name_en)
        return track.preview_url, True

    return None, False
