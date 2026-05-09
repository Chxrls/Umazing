"""
music/commands.py — All music slash commands registered to the command tree.

Commands:
  /play [title] [album] [version]
  /list category:[album|song|character] [character]
  /controls
  /nowplaying
  /queue
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Callable

import discord
from discord import app_commands

from .api import (
    fetch_all_albums,
    fetch_album,
    fetch_filter,
    fuzzy_match_album,
    get_album_art,
    get_track_preview,
    resolve_album_slug,
    resolve_character_slug,
    resolve_song_slug,
    search_songs,
    slugify,
    track_album_art,
    track_album_name,
    track_singers_str,
)
from .player import TrackInfo, get_player

log = logging.getLogger("umapyoi.music.commands")

# ── Embed colour constants ──────────────────────────────────────────────────
C_PLAY  = discord.Color.purple()
C_LIST  = discord.Color.blue()
C_ERROR = discord.Color.red()

ITEMS_PER_PAGE = 10
MAX_SELECT     = 25   # Discord API hard cap on Select options


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════

def error_embed(description: str, status: int | None = None) -> discord.Embed:
    title = f"⚠️ Error (HTTP {status})" if status else "⚠️ Error"
    return discord.Embed(title=title, description=description, color=C_ERROR)


def _track_to_info(track: dict, album: dict | None = None) -> TrackInfo:
    """Convert a raw API track dict to a TrackInfo.

    The /music/album/<id> endpoint wraps each track as:
      {disc_no, disc_order, runtime, track_id, track: {id, name_en, ...}}
    The /music/filter endpoint returns tracks flat:
      {id, name_en, preview_url, _singers, _albums, ...}

    This function handles both shapes.
    """
    # Unwrap the album-endpoint wrapper if present
    inner = track.get("track", track)
    runtime = track.get("runtime") or inner.get("runtime")

    if album:
        album_name = album.get("name_en", "Unknown Album")
        art = album.get("album_art")
    else:
        album_name = track_album_name(inner)
        art = track_album_art(inner)
    return TrackInfo(
        track_id=inner.get("id", 0),
        name_en=inner.get("name_en", "Unknown"),
        album_name=album_name,
        album_art=art,
        preview_url=get_track_preview(inner),
        singers=track_singers_str(inner),
        runtime=runtime,
    )


def _progress_bar(elapsed: int, total: int | None, width: int = 12) -> str:
    if not total or total <= 0:
        return "▬" * width + "●"
    ratio = min(elapsed / total, 1.0)
    filled = int(ratio * width)
    return "▬" * filled + "●" + "─" * (width - filled)


def _fmt_time(seconds: int | None) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


# ═══════════════════════════════════════════════════════════════════════════
# Interactive Views
# ═══════════════════════════════════════════════════════════════════════════

class AlbumBrowserView(discord.ui.View):
    """Shows a paginated album list with a Select dropdown per page."""

    def __init__(self, albums: list[dict], interaction: discord.Interaction):
        super().__init__(timeout=120)
        self.albums      = albums
        self.interaction = interaction
        self.page        = 0
        self._build_page()

    def _total_pages(self) -> int:
        return max(1, (len(self.albums) + MAX_SELECT - 1) // MAX_SELECT)

    def _page_albums(self) -> list[dict]:
        s = self.page * MAX_SELECT
        return self.albums[s:s + MAX_SELECT]

    def _build_page(self):
        self.clear_items()
        page_albs = self._page_albums()

        select = discord.ui.Select(
            placeholder="Select an album to browse…",
            options=[
                discord.SelectOption(
                    label=a.get("name_en", "Unknown")[:100],
                    value=str(a["id"]),
                    description=f"ID {a['id']}",
                )
                for a in page_albs
            ],
        )
        select.callback = self._select_callback
        self.add_item(select)

        if self._total_pages() > 1:
            prev_btn = discord.ui.Button(label="⬅ Prev", style=discord.ButtonStyle.secondary,
                                         disabled=(self.page == 0))
            next_btn = discord.ui.Button(label="➡ Next", style=discord.ButtonStyle.secondary,
                                         disabled=(self.page >= self._total_pages() - 1))
            prev_btn.callback = self._prev
            next_btn.callback = self._next
            self.add_item(prev_btn)
            self.add_item(next_btn)

    def make_embed(self) -> discord.Embed:
        page_albs = self._page_albums()
        lines = [
            f"`{self.page * MAX_SELECT + i + 1}.` {a.get('name_en', 'Unknown')}"
            for i, a in enumerate(page_albs)
        ]
        embed = discord.Embed(
            title="🎵 Umamusume Albums",
            description="\n".join(lines),
            color=C_LIST,
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self._total_pages()} · {len(self.albums)} albums"
        )
        return embed

    async def _select_callback(self, interaction: discord.Interaction):
        album_id = int(interaction.data["values"][0])
        await interaction.response.defer(thinking=True)
        status, album = await fetch_album(album_id)
        if status != 200 or not album:
            await interaction.followup.send(embed=error_embed("Could not fetch album.", status), ephemeral=True)
            return
        embed = _album_detail_embed(album)
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Auto-play / queue all album tracks if user is in a VC
        guild = interaction.guild
        member = interaction.user
        if guild and isinstance(member, discord.Member) and member.voice:
            player = get_player(guild.id)
            await player.connect(member.voice.channel)
            tracks = album.get("_tracks", [])
            if tracks:
                loop = asyncio.get_event_loop()
                all_infos = [_track_to_info(t, album) for t in tracks]
                if player.is_playing() or player.is_paused():
                    player.queue.extend(all_infos)
                else:
                    for t in all_infos[1:]:
                        player.queue.append(t)
                    await player.play_track(all_infos[0], loop)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._build_page()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self._total_pages() - 1, self.page + 1)
        self._build_page()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)


def _album_detail_embed(album: dict) -> discord.Embed:
    tracks = album.get("_tracks", [])
    lines = []
    for i, t in enumerate(tracks):
        # Tracks from /music/album/<id> are wrapped: {runtime, track: {name_en, ...}}
        inner = t.get("track", t)
        lines.append(f"`{i+1}.` {inner.get('name_en', '?')}")
    embed = discord.Embed(title=album.get("name_en", "Unknown Album"), color=C_LIST)
    embed.add_field(name=f"Tracks ({len(tracks)})", value="\n".join(lines[:40]) or "None", inline=False)
    if art := album.get("album_art"):
        embed.set_thumbnail(url=art)
    return embed


class PaginatedEmbedView(discord.ui.View):
    """Generic paginated embed with ⬅ ➡ buttons."""

    def __init__(self, pages: list[discord.Embed], timeout: float = 120):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.page  = 0
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.clear_items()
        prev = discord.ui.Button(label="⬅ Prev", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page == 0))
        nxt  = discord.ui.Button(label="➡ Next", style=discord.ButtonStyle.secondary,
                                  disabled=(self.page >= len(self.pages) - 1))
        prev.callback = self._prev
        nxt.callback  = self._next
        self.add_item(prev)
        self.add_item(nxt)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(len(self.pages) - 1, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)


def _paginate_items(items: list[str], title: str, footer_prefix: str,
                    color: discord.Color) -> list[discord.Embed]:
    pages = []
    total = len(items)
    n_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    for pg in range(n_pages):
        chunk = items[pg * ITEMS_PER_PAGE:(pg + 1) * ITEMS_PER_PAGE]
        embed = discord.Embed(title=title, description="\n".join(chunk), color=color)
        embed.set_footer(text=f"Page {pg+1}/{n_pages} · {footer_prefix} · {total} total")
        pages.append(embed)
    return pages


class ControlsView(discord.ui.View):
    """Persistent playback controls embed."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _get_player(self, interaction: discord.Interaction):
        if not interaction.guild:
            return None
        return get_player(interaction.guild.id)

    @discord.ui.button(label="⏸ Pause",  style=discord.ButtonStyle.primary,  custom_id="music_pause")
    async def pause(self, interaction: discord.Interaction, _):
        p = await self._get_player(interaction)
        if p: p.pause()
        await interaction.response.send_message("⏸ Paused.", ephemeral=True, delete_after=3)

    @discord.ui.button(label="▶ Resume", style=discord.ButtonStyle.success,  custom_id="music_resume")
    async def resume(self, interaction: discord.Interaction, _):
        p = await self._get_player(interaction)
        if p: p.resume()
        await interaction.response.send_message("▶ Resumed.", ephemeral=True, delete_after=3)

    @discord.ui.button(label="⏭ Skip",  style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip(self, interaction: discord.Interaction, _):
        p = await self._get_player(interaction)
        if p: p.skip()
        await interaction.response.send_message("⏭ Skipped.", ephemeral=True, delete_after=3)

    @discord.ui.button(label="⏹ Stop",  style=discord.ButtonStyle.danger,    custom_id="music_stop")
    async def stop(self, interaction: discord.Interaction, _):
        p = await self._get_player(interaction)
        if p: p.stop()
        await interaction.response.send_message("⏹ Stopped.", ephemeral=True, delete_after=3)

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.secondary, custom_id="music_shuffle")
    async def shuffle(self, interaction: discord.Interaction, _):
        p = await self._get_player(interaction)
        state = p.toggle_shuffle() if p else False
        await interaction.response.send_message(
            f"🔀 Shuffle {'ON' if state else 'OFF'}.", ephemeral=True, delete_after=3
        )

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.secondary, custom_id="music_loop")
    async def loop_btn(self, interaction: discord.Interaction, _):
        p = await self._get_player(interaction)
        state = p.toggle_loop() if p else False
        await interaction.response.send_message(
            f"🔁 Loop {'ON' if state else 'OFF'}.", ephemeral=True, delete_after=3
        )


# ═══════════════════════════════════════════════════════════════════════════
# Command registration helper
# ═══════════════════════════════════════════════════════════════════════════

def register_music_commands(tree: app_commands.CommandTree, record_command: Optional[Callable] = None) -> None:
    """Call once from bot setup_hook to add all music commands."""

    # ── /play ────────────────────────────────────────────────────────────────

    @tree.command(name="play", description="Play Umamusume music in your voice channel.")
    @app_commands.describe(
        title="Song title to search for",
        album="Album name or numeric ID to queue",
        version="Character name for a specific character version",
    )
    async def cmd_play(
        interaction: discord.Interaction,
        title: Optional[str] = None,
        album: Optional[str] = None,
        version: Optional[str] = None,
    ):
        if record_command:
            record_command("play", interaction)
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return  # stale or already acknowledged interaction; silently discard
        guild  = interaction.guild
        member = interaction.user

        # Ensure user is in a VC
        if not guild or not isinstance(member, discord.Member) or not member.voice:
            await interaction.followup.send(
                embed=error_embed("You need to be in a voice channel first.")
            )
            return

        vc_channel = member.voice.channel
        player = get_player(guild.id)
        loop   = asyncio.get_event_loop()

        # ── /play (bare) → browse albums ─────────────────────────────────────
        if not title and not album:
            await player.connect(vc_channel)
            all_albums = await fetch_all_albums()
            if not all_albums:
                await interaction.followup.send(embed=error_embed("Could not fetch albums."))
                return
            view = AlbumBrowserView(all_albums, interaction)
            await interaction.followup.send(embed=view.make_embed(), view=view)
            return

        # ── /play album:<name_or_id> ──────────────────────────────────────────
        if album and not title:
            if album.isdigit():
                status, alb = await fetch_album(int(album))
                if status != 200 or not alb:
                    await interaction.followup.send(embed=error_embed(f"Album ID {album} not found.", status))
                    return
            else:
                # Try live slug resolution first, then fuzzy fallback on full list
                alb_slug = await resolve_album_slug(album)
                if alb_slug:
                    # Fetch all albums to find by slug
                    all_albums = await fetch_all_albums()
                    alb_min = next((a for a in all_albums if a.get("slug") == alb_slug), None)
                else:
                    all_albums = await fetch_all_albums()
                    alb_min = fuzzy_match_album(all_albums, album)

                if not alb_min:
                    await interaction.followup.send(embed=error_embed(f"No album matching **{album}**."))
                    return
                status, alb = await fetch_album(alb_min["id"])
                if status != 200 or not alb:
                    await interaction.followup.send(embed=error_embed("Could not fetch album details."))
                    return

            await player.connect(vc_channel)
            tracks = alb.get("_tracks", [])
            if not tracks:
                await interaction.followup.send(embed=error_embed("This album has no tracks."))
                return

            all_infos = [_track_to_info(t, alb) for t in tracks]

            if player.is_playing() or player.is_paused():
                # Something is already playing — queue the whole album
                player.queue.extend(all_infos)
                embed = discord.Embed(
                    title="➕ Added to Queue",
                    description=f"**{alb.get('name_en', 'Unknown Album')}**",
                    color=C_PLAY,
                )
                embed.add_field(name="Tracks Queued", value=str(len(all_infos)))
                embed.add_field(name="Queue Length", value=str(len(player.queue)))
                if all_infos[0].album_art:
                    embed.set_thumbnail(url=all_infos[0].album_art)
            else:
                # Nothing playing — start immediately
                for t in all_infos[1:]:
                    player.queue.append(t)
                embed = discord.Embed(
                    title="🎵 Now Playing",
                    description=f"**{all_infos[0].name_en}**",
                    color=C_PLAY,
                )
                embed.add_field(name="Album", value=all_infos[0].album_name)
                embed.add_field(name="Queue", value=f"{len(player.queue)} tracks queued")
                if all_infos[0].album_art:
                    embed.set_thumbnail(url=all_infos[0].album_art)
                await player.play_track(all_infos[0], loop)

            await interaction.followup.send(embed=embed)
            return

        # ── /play title:<song> [version:<char|number>] ────────────────────────
        if title:
            # Resolve to the exact API slug from /filters (prevents HTTP 400)
            song_slug = await resolve_song_slug(title)

            if not song_slug:
                suggestions = await search_songs(title)
                if suggestions:
                    lines = [f"`{e['display']}`" for e in suggestions[:10]]
                    embed = discord.Embed(
                        title="🔍 Did you mean…",
                        description="\n".join(lines),
                        color=C_LIST,
                    )
                    embed.set_footer(text="Try one of the titles above with /play title:<name>")
                else:
                    embed = error_embed(
                        f"No song found matching **{title}**. "
                        "Check spelling or try a romanisation of the Japanese title."
                    )
                await interaction.followup.send(embed=embed)
                return

            # ── Fetch all versions first ──────────────────────────────────────
            status, data = await fetch_filter(song=song_slug)
            if status != 200 or data is None:
                await interaction.followup.send(embed=error_embed("API error fetching song.", status))
                return

            all_tracks = data.get("tracks", [])
            if not all_tracks:
                await interaction.followup.send(embed=error_embed(f"No results for **{title}**."))
                return

            # ── version is a number → pick by index from the list ─────────────
            if version and version.strip().isdigit():
                idx = int(version.strip()) - 1  # 1-based → 0-based
                if idx < 0 or idx >= len(all_tracks):
                    await interaction.followup.send(
                        embed=error_embed(
                            f"Invalid selection **{version}** — there are only {len(all_tracks)} version(s). "
                            f"Use a number between 1 and {len(all_tracks)}."
                        )
                    )
                    return
                chosen = all_tracks[idx]
                await player.connect(vc_channel)
                info = _track_to_info(chosen)
                if player.is_playing() or player.is_paused():
                    player.queue.append(info)
                    embed = discord.Embed(title="➕ Added to Queue", color=C_PLAY)
                    embed.add_field(name="Song",     value=info.name_en)
                    embed.add_field(name="Album",    value=info.album_name)
                    embed.add_field(name="Version",  value=f"#{int(version)} — {info.album_name}")
                    embed.add_field(name="Position", value=f"#{len(player.queue)} in queue")
                else:
                    embed = discord.Embed(title="🎵 Now Playing", color=C_PLAY)
                    embed.add_field(name="Song",    value=info.name_en)
                    embed.add_field(name="Album",   value=info.album_name)
                    embed.add_field(name="Version", value=f"#{int(version)} — {info.album_name}")
                    if info.singers:
                        embed.add_field(name="Characters", value=info.singers, inline=False)
                    await player.play_track(info, loop)
                if info.album_art:
                    embed.set_thumbnail(url=info.album_art)
                await interaction.followup.send(embed=embed)
                return

            # ── version is a character name → filter by character ─────────────
            char_slug: str | None = None
            if version:
                char_slug = await resolve_character_slug(version)
                if not char_slug:
                    await interaction.followup.send(
                        embed=error_embed(f"Character **{version}** not found. Check the name spelling.")
                    )
                    return
                # Re-fetch filtered by character
                status, data = await fetch_filter(song=song_slug, character=char_slug)
                if status != 200 or data is None:
                    await interaction.followup.send(embed=error_embed("API error fetching song.", status))
                    return
                all_tracks = data.get("tracks", [])
                if not all_tracks:
                    await interaction.followup.send(
                        embed=error_embed(f"No version of **{title}** found for character **{version}**.")
                    )
                    return

            # ── Multiple versions exist and no filter applied → show list ─────
            if len(all_tracks) > 1 and not version:
                lines = [
                    f"`{i+1}.` {t.get('name_en','?')} — {track_album_name(t)}"
                    for i, t in enumerate(all_tracks[:25])
                ]
                embed = discord.Embed(
                    title="🔍 Multiple Versions Found",
                    description="\n".join(lines),
                    color=C_LIST,
                )
                embed.set_footer(
                    text="Pick one with /play title:<song> version:<number>  "
                    "or narrow by character with version:<name>"
                )
                await interaction.followup.send(embed=embed)
                return

            await player.connect(vc_channel)
            info = _track_to_info(all_tracks[0])

            if player.is_playing() or player.is_paused():
                player.queue.append(info)
                embed = discord.Embed(title="➕ Added to Queue", color=C_PLAY)
                embed.add_field(name="Song",     value=info.name_en)
                embed.add_field(name="Album",    value=info.album_name)
                embed.add_field(name="Position", value=f"#{len(player.queue)} in queue")
                if version:
                    embed.add_field(name="Character Version", value=version.title())
            else:
                embed = discord.Embed(title="🎵 Now Playing", color=C_PLAY)
                embed.add_field(name="Song",  value=info.name_en)
                embed.add_field(name="Album", value=info.album_name)
                if version:
                    embed.add_field(name="Character Version", value=version.title())
                await player.play_track(info, loop)

            if info.album_art:
                embed.set_thumbnail(url=info.album_art)
            await interaction.followup.send(embed=embed)
            return

    # ── /list ─────────────────────────────────────────────────────────────────

    @tree.command(name="list", description="Browse albums, songs, or a character's tracks.")
    @app_commands.describe(
        category="What to list",
        character="Character name (required when category=character)",
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="album",     value="album"),
        app_commands.Choice(name="song",      value="song"),
        app_commands.Choice(name="character", value="character"),
    ])
    async def cmd_list(
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        character: Optional[str] = None,
    ):
        if record_command:
            record_command("list", interaction)
        try:
            await interaction.response.defer()
        except discord.NotFound:
            return  # stale interaction (bot reconnected); silently discard
        cat = category.value

        # ── album ──────────────────────────────────────────────────────────────
        if cat == "album":
            all_albums = await fetch_all_albums()
            if not all_albums:
                await interaction.followup.send(embed=error_embed("Could not fetch albums."))
                return
            items = [
                f"`{i+1}.` {a.get('name_en','?')}"
                for i, a in enumerate(all_albums)
            ]
            pages = _paginate_items(items, "📀 All Albums", "albums", C_LIST)
            view  = PaginatedEmbedView(pages)
            await interaction.followup.send(embed=pages[0], view=view)
            return

        # ── song ───────────────────────────────────────────────────────────────
        if cat == "song":
            all_albums = await fetch_all_albums()
            all_tracks: list[tuple[str, str]] = []
            for alb in all_albums:
                for t in alb.get("_tracks", []):
                    all_tracks.append((t.get("name_en", "?"), alb.get("name_en", "?")))
            if not all_tracks:
                await interaction.followup.send(embed=error_embed("Could not fetch songs."))
                return
            items = [f"`{i+1}.` {name} — {alb}" for i, (name, alb) in enumerate(all_tracks)]
            pages = _paginate_items(items, "🎶 All Songs", "songs", C_LIST)
            view  = PaginatedEmbedView(pages)
            await interaction.followup.send(embed=pages[0], view=view)
            return

        # ── character ──────────────────────────────────────────────────────────
        if cat == "character":
            if not character:
                await interaction.followup.send(
                    embed=error_embed("Please provide `character:<name>` when using category=character.")
                )
                return

            char_slug = await resolve_character_slug(character)
            if not char_slug:
                await interaction.followup.send(
                    embed=error_embed(f"Character **{character}** not found. Check the spelling.")
                )
                return

            status, data = await fetch_filter(character=char_slug)
            if status != 200 or data is None:
                await interaction.followup.send(embed=error_embed("Could not fetch character songs.", status))
                return

            tracks = data.get("tracks", [])
            if not tracks:
                await interaction.followup.send(
                    embed=error_embed(f"No songs found featuring **{character}**.")
                )
                return

            items = [
                f"`{i+1}.` {t.get('name_en','?')} — {track_album_name(t)}"
                for i, t in enumerate(tracks)
            ]
            pages = _paginate_items(
                items,
                f"Songs featuring {character.title()}",
                f"songs · {character.title()}",
                C_LIST,
            )
            view = PaginatedEmbedView(pages)
            await interaction.followup.send(embed=pages[0], view=view)
            return

    # ── /controls ─────────────────────────────────────────────────────────────

    @tree.command(name="controls", description="Show persistent playback controls.")
    async def cmd_controls(interaction: discord.Interaction):
        if record_command:
            record_command("controls", interaction)
        embed = discord.Embed(
            title="🎛️ Playback Controls",
            description="Use the buttons below to control playback.",
            color=C_PLAY,
        )
        try:
            await interaction.response.send_message(embed=embed, view=ControlsView())
        except discord.NotFound:
            return  # stale interaction after reconnect

    # ── /nowplaying ────────────────────────────────────────────────────────────

    @tree.command(name="nowplaying", description="Show the currently playing track.")
    async def cmd_nowplaying(interaction: discord.Interaction):
        if record_command:
            record_command("nowplaying", interaction)
        try:
            if not interaction.guild:
                await interaction.response.send_message(embed=error_embed("Guild only command."), ephemeral=True)
                return

            player  = get_player(interaction.guild.id)
            current = player.current

            if not current:
                await interaction.response.send_message(
                    embed=error_embed("Nothing is currently playing."), ephemeral=True
                )
                return

            elapsed  = player.elapsed_seconds()
            bar      = _progress_bar(elapsed, current.runtime)
            time_str = f"`{_fmt_time(elapsed)}` {bar} `{_fmt_time(current.runtime)}`"

            embed = discord.Embed(title="🎵 Now Playing", color=C_PLAY)
            embed.add_field(name="Song",       value=current.name_en,    inline=False)
            embed.add_field(name="Album",      value=current.album_name, inline=True)
            embed.add_field(name="Characters", value=current.singers or "—", inline=True)
            embed.add_field(name="Progress",   value=time_str,           inline=False)
            if current.is_preview:
                embed.add_field(name="⚠️ Preview Only", value="Full audio unavailable via yt-dlp.", inline=False)
            if current.album_art:
                embed.set_thumbnail(url=current.album_art)
            embed.set_footer(text=f"Loop: {'ON' if player.loop else 'OFF'} · Shuffle: {'ON' if player.shuffle else 'OFF'}")
            await interaction.response.send_message(embed=embed)
        except discord.NotFound:
            return  # stale interaction after reconnect

    # ── /queue ─────────────────────────────────────────────────────────────────

    @tree.command(name="queue", description="Show the current playback queue.")
    async def cmd_queue(interaction: discord.Interaction):
        if record_command:
            record_command("queue", interaction)
        try:
            if not interaction.guild:
                await interaction.response.send_message(embed=error_embed("Guild only command."), ephemeral=True)
                return

            player  = get_player(interaction.guild.id)
            queue   = player.queue
            current = player.current

            if not current and not queue:
                await interaction.response.send_message(
                    embed=error_embed("The queue is empty."), ephemeral=True
                )
                return

            items: list[str] = []
            if current:
                items.append(f"▶️ **{current.name_en}** — {current.album_name}")
            for i, t in enumerate(queue):
                items.append(f"`{i+1}.` {t.name_en} — {t.album_name}")

            pages = _paginate_items(items, "📋 Queue", "tracks", C_LIST)
            view  = PaginatedEmbedView(pages)
            await interaction.response.send_message(embed=pages[0], view=view)
        except discord.NotFound:
            return  # stale interaction after reconnect
