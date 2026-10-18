"""Notifier service module forwarding to bot.utils."""
from bot.utils import NotifierService, safe_edit_callback, safe_edit_message

__all__ = [
    "safe_edit_message",
    "safe_edit_callback",
    "NotifierService",
]
