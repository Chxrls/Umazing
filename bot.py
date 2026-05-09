from __future__ import annotations
import os
import logging
from difflib import SequenceMatcher
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

# Metrics — imported lazily to avoid circular import when web.py is not present
try:
    from web import metrics as _metrics
except ImportError:
    _metrics = None  # type: ignore[assignment]

# Music commands
from music import register_music_commands, set_session as music_set_session
from db import history_db


def _record_request(status: int) -> None:
    if _metrics is not None:
        _metrics.record_request(status)


def _record_command(name: str, interaction: Optional[discord.Interaction] = None) -> None:
    if _metrics is not None:
        _metrics.record_command(name)
    if interaction and interaction.guild:
        user_name = str(interaction.user)
        channel_name = interaction.channel.name if hasattr(interaction.channel, 'name') else str(interaction.channel.id)
        history_db.log_interaction(
            guild_id=str(interaction.guild.id),
            guild_name=interaction.guild.name,
            channel_id=str(interaction.channel.id),
            channel_name=channel_name,
            user_name=user_name,
            action_type="command",
            action_detail=name
        )

# Configuration
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional

if not TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN environment variable is missing. "
        "Copy .env.example → .env and fill in your bot token."
    )

API_BASE = "https://umapyoi.net/api/v1"
GAMETORA_SUPPORT_URL = "https://gametora.com/umamusume/support-cards"

# Embed theming
EMBED_COLOR = 0xEE6FAB # Sakura pink 
EMBED_ERROR_COLOR = 0xFF4444  # Red (errors)

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(message)s")
log = logging.getLogger("umapyoi")


intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Register music slash commands at import time
register_music_commands(tree, record_command=_record_command)

# Reusable aiohttp session — created on_ready, closed on shutdown
session: Optional[aiohttp.ClientSession] = None


# API helper functions

async def api_get(path: str) -> tuple[int, dict | list | None]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    log.debug("GET %s", url)
    async with session.get(url) as resp:
        status = resp.status
        _record_request(status)
        if status == 204:
            return 204, None
        if status != 200:
            return status, None
        return 200, await resp.json()


def error_embed(status: int, hint: str = "") -> discord.Embed:
    """Return a standard error embed for non-200 API responses."""
    embed = discord.Embed(
        title="⚠️  API Error",
        description=(
            f"The umapyoi.net API returned **HTTP {status}**.\n"
            f"{hint or 'Please try again in a moment.'}"
        ),
        color=EMBED_ERROR_COLOR,
    )
    embed.set_footer(text="Data from umapyoi.net")
    return embed


# Character name resolution 

# Cache the character list in memory so every slash command doesn't
# re-fetch the full list. It's refreshed once on_ready.
_character_cache: list[dict] = []


async def refresh_character_cache() -> None:
    """Fetch the full character list from /character/list and cache it."""
    global _character_cache
    status, data = await api_get("/character/list")
    if status == 200 and isinstance(data, list):
        _character_cache = data
        log.info("Character cache refreshed — %d characters loaded.", len(data))
    else:
        log.warning("Failed to refresh character cache (HTTP %d).", status)


def _normalise(s: str) -> str:
    """Normalise a string for fuzzy matching: lowercase, strip non-breaking spaces."""
    return s.lower().replace("\u00a0", " ").replace("\u200b", "").strip()


def resolve_character(name: str) -> tuple[dict | None, list[dict]]:
    """
    Fuzzy-match *name* against the cached character list.

    Returns ``(best_match_or_None, close_alternatives)``.
    The character list objects contain at minimum:
        id, name_en, name_jp, name_en_internal, thumb_img, color_main
    """
    query = _normalise(name)
    scored: list[tuple[float, dict]] = []

    for ch in _character_cache:
        # Match against English name
        en = _normalise(ch.get("name_en", ""))
        jp = ch.get("name_jp", "")
        internal = _normalise(ch.get("name_en_internal", ""))

        # Exact match shortcuts
        if query == en or query == internal or query == jp:
            return ch, []

        ratio = SequenceMatcher(None, query, en).ratio()
        # Boost if the query is a substring
        if query in en:
            ratio = max(ratio, 0.85)

        scored.append((ratio, ch))

    scored.sort(key=lambda t: t[0], reverse=True)

    if not scored:
        return None, []

    best_score, best = scored[0]
    if best_score < 0.5:
        return None, []

    # Collect close alternatives (within 0.05 of best, excluding best itself)
    alternatives = [ch for sc, ch in scored[1:6] if sc >= best_score - 0.05]
    return best, alternatives


# Helpers for each endpoints

async def fetch_character_info(chara_id: int) -> tuple[int, dict | None]:
    """GET /character/<chara_id> — full character profile."""
    return await api_get(f"/character/{chara_id}")


async def fetch_character_images(chara_id: int) -> tuple[int, list | None]:
    """GET /character/images/<chara_id> — character artwork categories."""
    return await api_get(f"/character/images/{chara_id}")


async def fetch_support_cards(game_id: int) -> tuple[int, list | None]:
    """GET /support/character/<game_id> — support cards for a character."""
    return await api_get(f"/support/character/{game_id}")


async def fetch_va_ids_for_character(chara_id: int) -> tuple[int, list | None]:
    """GET /va/character/<chara_id> — list of VA IDs for a character."""
    return await api_get(f"/va/character/{chara_id}")


async def fetch_va_info(va_id: int) -> tuple[int, dict | None]:
    """GET /va/<va_id> — voice actor details."""
    return await api_get(f"/va/{va_id}")


async def fetch_va_socials(va_id: int) -> tuple[int, list | None]:
    """GET /va/socials/<va_id> — social media links for a VA."""
    return await api_get(f"/va/socials/{va_id}")


# For slash commands sections

# CHARACTER
@tree.command(name="character", description="Look up an Umamusume character by name.")
@app_commands.describe(
    name="Character name (e.g. 'Sakura Bakushin O')",
    include="Specific field to return — omit for all fields",
)
@app_commands.choices(include=[
    app_commands.Choice(name="Info", value="info"),
    app_commands.Choice(name="Image", value="image"),
    app_commands.Choice(name="Birthday", value="birthday"),
])
async def cmd_character(
    interaction: discord.Interaction,
    name: str,
    include: Optional[app_commands.Choice[str]] = None,
):
    _record_command("character", interaction)
    try:
        await interaction.response.defer()
    except (discord.NotFound, discord.HTTPException):
        return

    # Resolve character name 
    match, alts = resolve_character(name)
    if match is None:
        embed = discord.Embed(
            title="Character Not Found",
            description=(
                f"Character **'{name}'** not found.\n"
                "Check spelling or try the Japanese name."
            ),
            color=EMBED_ERROR_COLOR,
        )
        await interaction.followup.send(embed=embed)
        return

    chara_id: int = match["id"]       # umapyoi internal row ID
    chara_name: str = match["name_en"]

    # Fetch character detail to get game_id and other info 
    status, info = await fetch_character_info(chara_id)
    if status != 200 or info is None:
        await interaction.followup.send(embed=error_embed(status))
        return

    game_id: int = info["game_id"]
    include_val = include.value if include else None

    embed = discord.Embed(
        title=chara_name,
        url=f"https://umapyoi.net/character/{match.get('preferred_url', '')}",
        color=int(match.get("color_main", "#EE6FAB").lstrip("#"), 16),
    )
    embed.add_field(name="Character ID", value=str(game_id), inline=True)

    # Thumbnail always
    if match.get("thumb_img"):
        embed.set_thumbnail(url=match["thumb_img"])

    # Ambiguity note
    if alts:
        alt_names = ", ".join(a["name_en"] for a in alts[:3])
        embed.set_footer(text=f"Close matches: {alt_names}")


    show_info = include_val in (None, "info")
    show_image = include_val in (None, "image")
    show_birthday = include_val in (None, "birthday")

    # Information field/s
    if show_info:
        embed.add_field(name="Japanese Name", value=info.get("name_jp", "—"), inline=True)
        embed.add_field(name="Grade", value=info.get("grade", "—"), inline=True)
        embed.add_field(name="Height", value=f"{info.get('height', '—')} cm", inline=True)
        embed.add_field(name="Residence", value=info.get("residence", "—"), inline=True)
        embed.add_field(name="Strengths", value=info.get("strengths", "—"), inline=True)
        embed.add_field(name="Weaknesses", value=info.get("weaknesses", "—"), inline=True)

        slogan = info.get("slogan")
        if slogan:
            embed.add_field(name="Catchphrase", value=slogan, inline=False)

        profile = info.get("profile")
        if profile:
            # Truncate long profiles for embed readability
            if len(profile) > 400:
                profile = profile[:397] + "…"
            embed.add_field(name="Profile", value=profile, inline=False)

    # Bdays
    if show_birthday:
        bday_m = info.get("birth_month")
        bday_d = info.get("birth_day")
        if bday_m and bday_d:
            embed.add_field(name="Birthday", value=f"{bday_m}/{bday_d}", inline=True)
        else:
            embed.add_field(name="Birthday", value="Unknown", inline=True)

    # Image
    if show_image:
        img_status, img_data = await fetch_character_images(chara_id)
        if img_status == 200 and img_data:
            # Use the first image in the first category (Uniform / latest)
            first_category = img_data[0]
            first_image_url = first_category["images"][0]["image"]
            embed.set_image(url=first_image_url)
        else:
            embed.add_field(name="Image", value="No images available.", inline=False)

    embed.set_footer(text=embed.footer.text or "Data from umapyoi.net")
    await interaction.followup.send(embed=embed)


# GAME CARDS

@tree.command(name="card", description="List support cards for an Umamusume character.")
@app_commands.describe(
    character="Character name (e.g. 'Sakura Bakushin O')",
    info="Include GameTora redirect links for each card",
)
async def cmd_card(
    interaction: discord.Interaction,
    character: str,
    info: Optional[bool] = False,
):
    _record_command("card", interaction)
    try:
        await interaction.response.defer()
    except (discord.NotFound, discord.HTTPException):
        return

    # Resolve character name
    match, alts = resolve_character(character)
    if match is None:
        embed = discord.Embed(
            title="Character Not Found",
            description=(
                f"Character **'{character}'** not found.\n"
                "Check spelling or try the Japanese name."
            ),
            color=EMBED_ERROR_COLOR,
        )
        await interaction.followup.send(embed=embed)
        return

    chara_id: int = match["id"]
    chara_name: str = match["name_en"]

    # Fetch full character detail to get game_id
    status, char_info = await fetch_character_info(chara_id)
    if status != 200 or char_info is None:
        await interaction.followup.send(embed=error_embed(status))
        return

    game_id: int = char_info["game_id"]

    # Fetch support cards
    sc_status, cards = await fetch_support_cards(game_id)
    if sc_status == 204 or cards is None or len(cards) == 0:
        embed = discord.Embed(
            title=f"Support Cards — {chara_name}",
            description="No support cards found for this character.",
            color=int(match.get("color_main", "#EE6FAB").lstrip("#"), 16),
        )
        embed.add_field(name="Character ID", value=str(game_id), inline=True)
        if match.get("thumb_img"):
            embed.set_thumbnail(url=match["thumb_img"])
        await interaction.followup.send(embed=embed)
        return

    if sc_status != 200:
        await interaction.followup.send(embed=error_embed(sc_status))
        return

    embed = discord.Embed(
        title=f"Support Cards — {chara_name}",
        color=int(match.get("color_main", "#EE6FAB").lstrip("#"), 16),
    )
    embed.add_field(name="Character ID", value=str(game_id), inline=True)
    embed.add_field(name="Total Cards", value=str(len(cards)), inline=True)

    if match.get("thumb_img"):
        embed.set_thumbnail(url=match["thumb_img"])

    # Build card listing
    lines: list[str] = []
    for card in cards:
        title_en = card.get("title_en", card.get("title", "Unknown"))
        rarity = card.get("rarity_string", "?")
        card_type = card.get("type", "?")
        gametora_id = card.get("gametora", "")

        label = f"**{rarity}** {title_en} ({card_type})"

        if info and gametora_id:
            gt_url = f"{GAMETORA_SUPPORT_URL}/{gametora_id}"
            lines.append(f"• [{label}]({gt_url})")
        else:
            lines.append(f"• {label}")

    # Discord embeds have a 4096 char description limit; split if needed
    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:3997] + "…"

    embed.description = description

    if alts:
        alt_names = ", ".join(a["name_en"] for a in alts[:3])
        embed.set_footer(text=f"Close matches: {alt_names}")
    else:
        embed.set_footer(text="Data from umapyoi.net")

    await interaction.followup.send(embed=embed)


# UMAVOICE

@tree.command(name="umavoice", description="Find the voice actor for an Umamusume character.")
@app_commands.describe(
    character="Character name (e.g. 'Sakura Bakushin O')",
)
async def cmd_umavoice(
    interaction: discord.Interaction,
    character: str,
):
    _record_command("umavoice", interaction)
    try:
        await interaction.response.defer()
    except (discord.NotFound, discord.HTTPException):
        return

    # Resolve char name
    match, alts = resolve_character(character)
    if match is None:
        embed = discord.Embed(
            title="Character Not Found",
            description=(
                f"Character **'{character}'** not found.\n"
                "Check spelling or try the Japanese name."
            ),
            color=EMBED_ERROR_COLOR,
        )
        await interaction.followup.send(embed=embed)
        return

    chara_id: int = match["id"]
    chara_name: str = match["name_en"]

    # Fetch full character detail to get game_id
    status, char_info = await fetch_character_info(chara_id)
    if status != 200 or char_info is None:
        await interaction.followup.send(embed=error_embed(status))
        return

    game_id: int = char_info["game_id"]

    # Fetch VA IDs for each character
    va_status, va_ids = await fetch_va_ids_for_character(chara_id)
    if va_status != 200 or not va_ids:
        embed = discord.Embed(
            title=f"Voice Actor — {chara_name}",
            description="No voice actor information found for this character.",
            color=int(match.get("color_main", "#EE6FAB").lstrip("#"), 16),
        )
        embed.add_field(name="Character ID", value=str(game_id), inline=True)
        await interaction.followup.send(embed=embed)
        return

    # Typically a character has one VA, but the API returns a list
    va_id = va_ids[0]

    # VA inf
    info_status, va_info = await fetch_va_info(va_id)
    if info_status != 200 or va_info is None:
        await interaction.followup.send(embed=error_embed(info_status))
        return

    # VA Soc
    soc_status, socials = await fetch_va_socials(va_id)

    # If socials are empty
    if soc_status != 200:
        socials = []

    embed = discord.Embed(
        title=f"Voice Actor — {chara_name}",
        color=int(match.get("color_main", "#EE6FAB").lstrip("#"), 16),
    )
    embed.add_field(name="Character ID", value=str(game_id), inline=True)
    embed.add_field(name="VA Name", value=va_info.get("name_en", "Unknown"), inline=True)
    embed.add_field(name="VA Name (JP)", value=va_info.get("name_jp", "—"), inline=True)

    # Birthday
    va_bday_m = va_info.get("birth_month")
    va_bday_d = va_info.get("birth_day")
    va_bday_y = va_info.get("birth_year")
    if va_bday_m and va_bday_d:
        bday_str = f"{va_bday_y}/{va_bday_m}/{va_bday_d}" if va_bday_y else f"{va_bday_m}/{va_bday_d}"
        embed.add_field(name="VA Birthday", value=bday_str, inline=True)

    # VA image
    va_image = va_info.get("image")
    if va_image:
        embed.set_thumbnail(url=va_image)

    # Social links
    if socials:
        for social in socials:
            social_name = social.get("social_name", "Link")
            url = social.get("url", "")
            display = social.get("display_text") or url
            if url:
                embed.add_field(
                    name=social_name,
                    value=f"[{display}]({url})",
                    inline=True,
                )
    else:
        embed.add_field(name="Socials", value="No social links available.", inline=False)

    if alts:
        alt_names = ", ".join(a["name_en"] for a in alts[:3])
        embed.set_footer(text=f"Close matches: {alt_names}")
    else:
        embed.set_footer(text="Via Umapyoi")

    await interaction.followup.send(embed=embed)


# Bot events

# NOTE: on_ready is registered by main.py when running as a Web Service.
# The standalone __main__ block below registers its own on_ready for
# direct testing (python bot.py) without the Flask dashboard.


async def _standalone_on_ready():
    """on_ready used only when bot.py is run directly (not via main.py)."""
    global session
    session = aiohttp.ClientSession(
        headers={"User-Agent": "UmapyoiDiscordBot/1.0 (aiohttp)"},
        timeout=aiohttp.ClientTimeout(total=15),
    )
    await refresh_character_cache()
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s.", GUILD_ID)
    else:
        await tree.sync()
        log.info("Slash commands synced globally (may take up to 1 hour).")
    log.info("Bot ready as %s (ID %s).", bot.user, bot.user.id)


async def close_session():
    """Cleanly close the aiohttp session."""
    global session
    if session and not session.closed:
        await session.close()
        session = None


# Global error handler

@tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    log.exception("Unhandled command error: %s", error)

    embed = discord.Embed(
        title="⚠️  Something Went Wrong",
        description=(
            "An unexpected error occurred while processing your command.\n"
            "Please try again in a moment."
        ),
        color=EMBED_ERROR_COLOR,
    )
    embed.set_footer(text="If this persists, contact the bot maintainer.")

    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass  # Interaction may have expired

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if bot.user in message.mentions and message.guild:
        channel_name = message.channel.name if hasattr(message.channel, 'name') else str(message.channel.id)
        history_db.log_interaction(
            guild_id=str(message.guild.id),
            guild_name=message.guild.name,
            channel_id=str(message.channel.id),
            channel_name=channel_name,
            user_name=str(message.author),
            action_type="mention",
            action_detail=message.content[:50]
        )

# Entry point (standalone — without Flask dashboard)

def main():
    """Run the bot standalone (no Flask dashboard). Used for local testing."""
    bot.event(_standalone_on_ready)  # register the standalone on_ready

    async def runner():
        async with bot:
            try:
                await bot.start(TOKEN)
            finally:
                await close_session()

    import asyncio
    asyncio.run(runner())


if __name__ == "__main__":
    main()
