"""
main.py — Single entrypoint for Render Web Service deployment.

Start order:
  1. Flask web server  (daemon thread on PORT, default 5000)
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

from web import app, metrics, set_bot

log = logging.getLogger("umapyoi.main")

# ---------------------------------------------------------------------------
# Import everything the bot needs from bot.py
# ---------------------------------------------------------------------------
from bot import (  # noqa: E402
    TOKEN,
    GUILD_ID,
    bot,
    tree,
    refresh_character_cache,
)

# ---------------------------------------------------------------------------
# Flask thread
# ---------------------------------------------------------------------------

def run_flask() -> None:
    port = int(os.getenv("PORT", 5000))
    log.info("Starting Flask dashboard on port %d", port)
    # use_reloader=False is critical inside a thread
    app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False)


# ---------------------------------------------------------------------------
# Bot runner (same logic as bot.py main(), now separated)
# ---------------------------------------------------------------------------

async def bot_runner() -> None:
    loop = asyncio.get_running_loop()

    session: aiohttp.ClientSession | None = None

    @bot.event
    async def on_ready():  # type: ignore[override]
        nonlocal session

        session = aiohttp.ClientSession(
            headers={"User-Agent": "UmapyoiDiscordBot/1.0 (aiohttp)"},
            timeout=aiohttp.ClientTimeout(total=15),
        )

        # Inject into bot.py module so api_get() can use it
        import bot as bot_module
        bot_module.session = session

        await refresh_character_cache()

        # Inject bot + loop references into web.py for /api/* routes
        set_bot(bot, loop)

        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            await tree.sync(guild=guild_obj)
            log.info("Slash commands synced to guild %s.", GUILD_ID)
        else:
            await tree.sync()
            log.info("Slash commands synced globally.")

        log.info("Bot ready as %s (ID %s).", bot.user, bot.user.id)

    try:
        await bot.start(TOKEN)
    finally:
        if session and not session.closed:
            await session.close()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    )

    # Start Flask in a background daemon thread
    flask_thread = threading.Thread(target=run_flask, daemon=True, name="flask-dashboard")
    flask_thread.start()

    # Run the bot on the main thread
    asyncio.run(bot_runner())
