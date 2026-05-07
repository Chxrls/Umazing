"""
main.py — Single entrypoint for Render Web Service deployment.

Start order:
  1. Flask web server  (daemon thread on $PORT, default 5000)
  2. Discord bot       (main thread, async event loop)

Flask keeps Render happy (the process binds a port).
UptimeRobot pings GET /health every 5 min to prevent free-tier sleep.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import aiohttp
import discord

# Configure logging FIRST before any other imports that may log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
log = logging.getLogger("umapyoi.main")

# ---------------------------------------------------------------------------
# Import bot internals and Flask app
# ---------------------------------------------------------------------------
from web import app, set_bot  # noqa: E402

from bot import (  # noqa: E402
    TOKEN,
    GUILD_ID,
    bot,
    tree,
    refresh_character_cache,
)

# ---------------------------------------------------------------------------
# Module-level session so on_ready can set it and bot_runner can close it
# ---------------------------------------------------------------------------
_http_session: aiohttp.ClientSession | None = None


# ---------------------------------------------------------------------------
# on_ready — registered at module level (not inside a closure)
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    global _http_session

    _http_session = aiohttp.ClientSession(
        headers={"User-Agent": "UmapyoiDiscordBot/1.0 (aiohttp)"},
        timeout=aiohttp.ClientTimeout(total=15),
    )

    # Inject the session into bot.py so api_get() can use it
    import bot as bot_module
    bot_module.session = _http_session

    # Populate character cache
    await refresh_character_cache()

    # Inject bot + running loop into web.py for the /api/* routes
    loop = asyncio.get_running_loop()
    set_bot(bot, loop)

    # Sync slash commands
    if GUILD_ID:
        guild_obj = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild_obj)
        await tree.sync(guild=guild_obj)
        log.info("Slash commands synced to guild %s.", GUILD_ID)
    else:
        await tree.sync()
        log.info("Slash commands synced globally.")

    log.info("Bot ready as %s (ID %s).", bot.user, bot.user.id)


# ---------------------------------------------------------------------------
# Flask thread
# ---------------------------------------------------------------------------

def run_flask() -> None:
    port = int(os.getenv("PORT", 5000))
    log.info("Starting Flask dashboard on port %d", port)
    app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False)


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------

async def bot_runner() -> None:
    try:
        await bot.start(TOKEN)
    finally:
        if _http_session and not _http_session.closed:
            await _http_session.close()
            log.info("aiohttp session closed.")


# ---------------------------------------------------------------------------
# Entry point — works whether run directly OR via Render's start command
# ---------------------------------------------------------------------------

def main() -> None:
    flask_thread = threading.Thread(target=run_flask, daemon=True, name="flask-dashboard")
    flask_thread.start()
    asyncio.run(bot_runner())


if __name__ == "__main__":
    main()
