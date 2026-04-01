"""
Shared state and path helpers for Telegram bot route handlers.
"""

import asyncio
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Base directory for resolving relative resource paths (same as CLI)
_trns_home = os.path.abspath(os.environ.get("TRNS_HOME", os.getcwd()))


def _resolve_args_paths(args):
    """Resolve relative file paths in args against TRNS_HOME."""

    def _resolve(path):
        if path and not os.path.isabs(path):
            return os.path.join(_trns_home, path)
        return path

    args.lm_api_key_file = _resolve(getattr(args, "lm_api_key_file", "api_key.txt"))
    args.lm_prompt_file = _resolve(getattr(args, "lm_prompt_file", "prompt.md"))
    args.lm_prompt_original_file = _resolve(getattr(args, "lm_prompt_original_file", "prompt_original.md"))
    args.metadata_path = _resolve(getattr(args, "metadata_path", "metadata.json"))
    if getattr(args, "save_transcript", None):
        args.save_transcript = _resolve(args.save_transcript)
    return args


# User states
STATE_WAITING_KEY = "waiting_key"
STATE_WAITING_CONTEXT = "waiting_context"
STATE_PROCESSING = "processing"

# Store active processing tasks per user
user_processing_tasks = {}
processing_lock = threading.Lock()


def _remove_task_if_owner(user_id: int, job_id: str) -> bool:
    """Remove task entry only if this job still owns it (job_id matches)."""
    with processing_lock:
        info = user_processing_tasks.get(user_id)
        if info is not None and info.get("job_id") == job_id:
            del user_processing_tasks[user_id]
            logger.info(f"Cleaned up processing tasks for user {user_id} (job {job_id[:8]})")
            return True
    return False


# Store user states (in-memory, can be moved to persistent storage if needed)
user_states = {}
_user_states_lock = threading.Lock()


def get_user_state(user_id: int) -> Optional[str]:
    """Get user state (thread-safe)"""
    with _user_states_lock:
        return user_states.get(user_id)


def set_user_state(user_id: int, state: Optional[str]):
    """Set user state (thread-safe)"""
    with _user_states_lock:
        if state is None:
            user_states.pop(user_id, None)
        else:
            user_states[user_id] = state


def handle_task_error(task: asyncio.Task, user_id: int):
    """Handle errors from background tasks"""
    try:
        task.result()  # This will raise if task had an exception
    except asyncio.CancelledError:
        logger.debug(f"Task for user {user_id} was cancelled")
    except Exception as e:
        logger.exception(f"Error in background task for user {user_id}: {e}")
