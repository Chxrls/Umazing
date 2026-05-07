# music/__init__.py
"""Music package — exposes the setup function called by bot.py."""
from .commands import register_music_commands
from .api import set_session

__all__ = ["register_music_commands", "set_session"]
