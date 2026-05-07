# 🏇 Umapyoi Discord Bot

A Discord bot for looking up **Umamusume: Pretty Derby** characters, support cards, and voice actors — powered by the [umapyoi.net](https://umapyoi.net) public API.

## Features

| Command | Description |
|---------|-------------|
| `/character <name> [include]` | Look up a character's info, image, and birthday |
| `/card <character> [info]` | List all support cards for a character |
| `/umavoice <character>` | Find a character's voice actor and social links |

## Setup

### 1. Prerequisites
- Python 3.10+
- A Discord bot token ([create one here](https://discord.com/developers/applications))

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
# Edit .env and add your DISCORD_BOT_TOKEN
```

### 4. Run
```bash
python bot.py
```

> **Note:** Slash commands may take up to 1 hour to appear globally. To speed things up during development, set `DISCORD_GUILD_ID` in your `.env` file.

## Command Details

### `/character`
- **name** (required) — Character name, e.g. `Sakura Bakushin O`
- **include** (optional) — `Info`, `Image`, or `Birthday`. Omit for all fields.

### `/card`
- **character** (required) — Character name
- **info** (optional, default `False`) — If `True`, includes GameTora links for each card

### `/umavoice`
- **character** (required) — Character name

## API Rate Limits
The umapyoi.net API enforces: **10 req/s · 500 req/min · 7,200 req/hr**

## License
Personal/community use. Not affiliated with Cygames or umapyoi.net.
