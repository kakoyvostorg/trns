"""
Telegram bot route handlers — package facade.

Public API matches the former ``trns.bot.routes`` module for backward compatibility.
"""

from ._commands import cancel_user_processing
from ._dispatcher import route_update
from ._state import (
    _remove_task_if_owner,
    processing_lock,
    user_processing_tasks,
)

__all__ = [
    "cancel_user_processing",
    "route_update",
    "user_processing_tasks",
    "processing_lock",
    "_remove_task_if_owner",
]
