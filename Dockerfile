# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.13-slim

# ── System dependencies (ffmpeg is the critical one for discord.py audio) ────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── App directory ─────────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy source ───────────────────────────────────────────────────────────────
COPY . .

# ── Start the bot ─────────────────────────────────────────────────────────────
CMD ["python", "main.py"]
