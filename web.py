"""
web.py — Flask dashboard for the Umapyoi Discord bot.

Runs in a background daemon thread alongside the bot.
Provides:
  - GET  /            → Dashboard UI (login-gated)
  - GET  /login       → Login page
  - POST /login       → Authenticate
  - GET  /logout      → Clear session
  - GET  /health      → Health check for UptimeRobot
  - GET  /api/metrics → JSON metrics snapshot
  - GET  /api/channels → Guild channels list for announcement UI
  - POST /api/send-message → Send announcement via bot
"""

from __future__ import annotations

import asyncio
import os
import time
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
import logging
import queue
import json

from db import history_db

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_from_directory,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-me-in-production")

# ---------------------------------------------------------------------------
# Auth config (set via env vars)
# ---------------------------------------------------------------------------
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "admin")

# ---------------------------------------------------------------------------
# Thread-safe metrics store
# ---------------------------------------------------------------------------

class MetricsStore:
    """Central, thread-safe metrics collector for the dashboard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.start_time: float = time.time()
        self.total_requests: int = 0
        self.total_commands: int = 0
        # Sliding window of (timestamp, status_code) tuples for the last 24 h
        self._request_log: deque[tuple[float, int]] = deque()
        # Per-command call counts
        self.command_counts: dict[str, int] = defaultdict(int)
        # Per-status-code error counts
        self.error_counts: dict[str, int] = defaultdict(int)
        # Recent announcements (last 10)
        self.announcements: deque[dict] = deque(maxlen=10)

    # ------------------------------------------------------------------
    # Record helpers (called from bot.py)
    # ------------------------------------------------------------------

    def record_request(self, status: int) -> None:
        with self._lock:
            self.total_requests += 1
            now = time.time()
            self._request_log.append((now, status))
            # Prune entries older than 24 h
            cutoff = now - 86_400
            while self._request_log and self._request_log[0][0] < cutoff:
                self._request_log.popleft()
            if status not in (200, 204):
                self.error_counts[str(status)] = self.error_counts.get(str(status), 0) + 1

    def record_command(self, name: str) -> None:
        with self._lock:
            self.total_commands += 1
            self.command_counts[name] = self.command_counts.get(name, 0) + 1

    def record_announcement(self, channel_name: str, content: str) -> None:
        with self._lock:
            self.announcements.appendleft(
                {
                    "channel": channel_name,
                    "content": content[:120] + ("…" if len(content) > 120 else ""),
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                }
            )

    def reset(self) -> None:
        with self._lock:
            self.total_requests = 0
            self.total_commands = 0
            self._request_log.clear()
            self.command_counts.clear()
            self.error_counts.clear()
            self.start_time = time.time()

    # ------------------------------------------------------------------
    # Snapshot for the API
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            # Build 24 hourly buckets (index 0 = oldest hour)
            buckets: list[int] = [0] * 24
            cutoff = now - 86_400
            for ts, _status in self._request_log:
                if ts >= cutoff:
                    hour_idx = int((ts - cutoff) // 3_600)
                    if 0 <= hour_idx < 24:
                        buckets[hour_idx] += 1

            uptime_seconds = int(now - self.start_time)
            hours, rem = divmod(uptime_seconds, 3600)
            minutes, seconds = divmod(rem, 60)

            return {
                "total_requests": self.total_requests,
                "total_commands": self.total_commands,
                "uptime": f"{hours:02d}h {minutes:02d}m {seconds:02d}s",
                "uptime_seconds": uptime_seconds,
                "requests_by_hour": buckets,
                "command_counts": dict(self.command_counts),
                "error_counts": dict(self.error_counts),
                "announcements": list(self.announcements),
            }


# Singleton — imported by bot.py
metrics = MetricsStore()

# ---------------------------------------------------------------------------
# Real-time Log Collector for SSE
# ---------------------------------------------------------------------------

class LogCollector(logging.Handler):
    def __init__(self, maxlen=300):
        super().__init__()
        self.history = deque(maxlen=maxlen)
        self.clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        
    def emit(self, record):
        try:
            msg = self.format(record)
            log_entry = {
                "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": msg
            }
            with self._lock:
                self.history.append(log_entry)
                for q in self.clients:
                    try:
                        q.put_nowait(log_entry)
                    except queue.Full:
                        pass
        except Exception:
            self.handleError(record)

    def subscribe(self) -> tuple[queue.Queue, list[dict]]:
        q = queue.Queue(maxsize=100)
        with self._lock:
            self.clients.append(q)
            hist = list(self.history)
        return q, hist

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self.clients:
                self.clients.remove(q)

log_collector = LogCollector()
log_collector.setFormatter(logging.Formatter("%(message)s"))
# Attach it to the root logger so we get EVERYTHING (Flask, bot.py, etc)
logging.getLogger().addHandler(log_collector)

# ---------------------------------------------------------------------------
# Bot / event-loop references (injected by main.py after startup)
# ---------------------------------------------------------------------------
_bot_ref = None          # discord.Client instance
_bot_loop_ref = None     # the running asyncio event loop


def set_bot(bot_obj, loop: asyncio.AbstractEventLoop) -> None:
    """Called from main.py once the bot is ready to inject dependencies."""
    global _bot_ref, _bot_loop_ref
    _bot_ref = bot_obj
    _bot_loop_ref = loop


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory(os.path.join(app.root_path, 'assets'), filename)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": int(time.time())})


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == DASHBOARD_USER and password == DASHBOARD_PASS:
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/metrics")
@login_required
def api_metrics():
    return jsonify(metrics.snapshot())


@app.route("/api/metrics/reset", methods=["POST"])
@login_required
def api_metrics_reset():
    metrics.reset()
    return jsonify({"ok": True})


@app.route("/api/channels")
@login_required
def api_channels():
    if _bot_ref is None:
        return jsonify({"error": "Bot not ready yet."}), 503

    channels = []
    for guild in _bot_ref.guilds:
        for ch in guild.text_channels:
            channels.append(
                {
                    "id": str(ch.id),
                    "name": f"#{ch.name}",
                    "guild": guild.name,
                }
            )
    return jsonify({"channels": channels})


@app.route("/api/send-message", methods=["POST"])
@login_required
def api_send_message():
    if _bot_ref is None or _bot_loop_ref is None:
        return jsonify({"error": "Bot not ready yet."}), 503

    data = request.get_json(force=True, silent=True) or {}
    channel_id = data.get("channel_id")
    content = (data.get("content") or "").strip()

    if not channel_id or not content:
        return jsonify({"error": "channel_id and content are required."}), 400

    if len(content) > 2000:
        return jsonify({"error": "Message exceeds Discord's 2000 character limit."}), 400

    async def _send():
        channel = _bot_ref.get_channel(int(channel_id))
        if channel is None:
            raise ValueError(f"Channel {channel_id} not found.")
        await channel.send(content)
        return channel.name

    try:
        future = asyncio.run_coroutine_threadsafe(_send(), _bot_loop_ref)
        channel_name = future.result(timeout=10)
        metrics.record_announcement(channel_name, content)
        return jsonify({"ok": True, "channel": f"#{channel_name}"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except TimeoutError:
        return jsonify({"error": "Bot timed out sending the message."}), 504
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Unexpected error: {exc}"}), 500

@app.route("/api/logs/stream")
@login_required
def api_logs_stream():
    """SSE endpoint to stream logs to the dashboard in real-time."""
    def event_stream():
        q, hist = log_collector.subscribe()
        try:
            # Send recent history first
            for entry in hist:
                yield f"data: {json.dumps(entry)}\n\n"
            
            # Loop forever waiting for new logs
            while True:
                try:
                    entry = q.get(timeout=15)
                    yield f"data: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    # Send a keep-alive ping so the browser doesn't drop the connection
                    yield ": keep-alive\n\n"
        finally:
            log_collector.unsubscribe(q)
            
    response = Response(event_stream(), mimetype="text/event-stream")
    # Prevent buffering in proxy servers like nginx (if any)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response

@app.route("/api/history/servers")
@login_required
def api_history_servers():
    return jsonify({"servers": history_db.get_servers()})

@app.route("/api/history/channels/<guild_id>")
@login_required
def api_history_channels(guild_id):
    return jsonify({"channels": history_db.get_channels(guild_id)})

@app.route("/api/history/logs/<channel_id>")
@login_required
def api_history_logs(channel_id):
    logs = history_db.get_logs(channel_id, limit=100)
    graph = history_db.get_channel_activity_graph(channel_id)
    return jsonify({"logs": logs, "graph": graph})

