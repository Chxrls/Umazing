import sqlite3
import threading
import time
import os
from typing import List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "history.db")

class HistoryDB:
    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(DB_PATH, check_same_thread=False)

    def _init_db(self):
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS interactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id TEXT,
                        guild_name TEXT,
                        channel_id TEXT,
                        channel_name TEXT,
                        user_name TEXT,
                        action_type TEXT,
                        action_detail TEXT,
                        timestamp REAL
                    )
                """)
                conn.commit()

    def log_interaction(self, guild_id: str, guild_name: str, channel_id: str, channel_name: str, user_name: str, action_type: str, action_detail: str):
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO interactions 
                    (guild_id, guild_name, channel_id, channel_name, user_name, action_type, action_detail, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (guild_id, guild_name, channel_id, channel_name, user_name, action_type, action_detail, time.time()))
                conn.commit()

    def get_servers(self) -> List[Dict[str, str]]:
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT DISTINCT guild_id, guild_name FROM interactions WHERE guild_id IS NOT NULL")
                return [{"id": row[0], "name": row[1]} for row in cursor.fetchall()]

    def get_channels(self, guild_id: str) -> List[Dict[str, str]]:
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT DISTINCT channel_id, channel_name FROM interactions WHERE guild_id = ?", (guild_id,))
                return [{"id": row[0], "name": row[1]} for row in cursor.fetchall()]

    def get_logs(self, channel_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("""
                    SELECT user_name, action_type, action_detail, timestamp 
                    FROM interactions 
                    WHERE channel_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (channel_id, limit))
                return [{
                    "user_name": row[0],
                    "action_type": row[1],
                    "action_detail": row[2],
                    "timestamp": row[3]
                } for row in cursor.fetchall()]

    def get_channel_activity_graph(self, channel_id: str) -> List[int]:
        # Returns counts for the last 24 hours, grouped by hour (0 = oldest, 23 = newest)
        now = time.time()
        cutoff = now - 86400
        buckets = [0] * 24
        
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT timestamp FROM interactions WHERE channel_id = ? AND timestamp >= ?", (channel_id, cutoff))
                for row in cursor.fetchall():
                    ts = row[0]
                    hour_idx = int((ts - cutoff) // 3600)
                    if 0 <= hour_idx < 24:
                        buckets[hour_idx] += 1
        return buckets

# Global instance
history_db = HistoryDB()
