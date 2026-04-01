"""
Telegram bot slash commands: /start, /cancel, /stats and cooperative job cancellation.
"""

import asyncio
import logging

from pyrogram import Client
from pyrogram.types import Message

from trns.bot.utils import (
    load_metadata,
    get_text,
    is_user_authenticated,
    reset_context,
    get_daily_capacity,
    DAILY_CAPACITY,
    WARNING_THRESHOLD,
)

from ._state import (
    STATE_WAITING_KEY,
    processing_lock,
    user_processing_tasks,
    _remove_task_if_owner,
    set_user_state,
)

logger = logging.getLogger(__name__)


async def start_command(client: Client, message: Message) -> None:
    """Handle /start command - authentication flow"""
    from trns.bot.server import bot_metadata, create_keyboard
    from trns.bot.utils import initialize_user_settings

    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()

    # Check if already authenticated
    if is_user_authenticated(user_id):
        # Initialize user settings if not already done
        initialize_user_settings(user_id)

        # Create personalized keyboard for this user
        keyboard = create_keyboard(metadata, user_id)

        await message.reply_text(
            get_text(metadata, "auth_success"),
            reply_markup=keyboard
        )
        return

    # Start authentication flow
    set_user_state(user_id, STATE_WAITING_KEY)
    await message.reply_text(get_text(metadata, "start_message"))


async def cancel_command(client: Client, message: Message) -> None:
    """Handle /cancel command"""
    from trns.bot.server import bot_metadata, create_keyboard

    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()
    keyboard = create_keyboard(metadata, user_id)

    # Cancel any ongoing processing
    await cancel_user_processing(user_id)

    # Reset context
    reset_context(user_id)

    # Clear user state
    set_user_state(user_id, None)

    await message.reply_text(
        get_text(metadata, "cancel_success"),
        reply_markup=keyboard
    )


async def stats_command(client: Client, message: Message) -> None:
    """Handle /stats command - show remaining daily capacity"""
    from trns.bot.server import bot_metadata

    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()

    # Check authentication
    if not is_user_authenticated(user_id):
        await message.reply_text(get_text(metadata, "not_authenticated"))
        return

    # Get current capacity
    capacity = get_daily_capacity()

    # Format message
    stats_text = f"📊 Статистика:\n\nОсталось запросов сегодня: {capacity} / {DAILY_CAPACITY}"

    if capacity < WARNING_THRESHOLD:
        stats_text += f"\n⚠️ Внимание: осталось менее {WARNING_THRESHOLD} запросов!"

    await message.reply_text(stats_text)


async def cancel_user_processing(user_id: int):
    """Cancel ongoing processing for a user"""
    # Get task info outside lock to avoid holding lock during async operations
    task_info = None
    with processing_lock:
        if user_id in user_processing_tasks:
            task_info = user_processing_tasks[user_id].copy()

    if not task_info:
        return

    expected_job_id = task_info.get("job_id")

    # Set shutdown flag first to stop pipeline
    shutdown_flag = task_info.get("shutdown_flag")
    if shutdown_flag:
        shutdown_flag.set()
        logger.info(f"Shutdown flag set for user {user_id}")

    # Cancel output task first (it depends on shutdown_flag)
    output_task = task_info.get("output_task")
    if output_task and not output_task.done():
        logger.info(f"Cancelling output task for user {user_id}")
        output_task.cancel()
        try:
            await asyncio.wait_for(output_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(f"Output task cancellation timeout for user {user_id}")

    # Note: Executor tasks (threads) can't be cancelled directly
    # We rely on the shutdown_flag to stop the pipeline
    # The pipeline checks shutdown_flag regularly and will stop when it's set
    executor_task = task_info.get("executor_task")
    if executor_task and not executor_task.done():
        logger.info(f"Waiting for executor task to finish for user {user_id}")
        try:
            await asyncio.wait_for(executor_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(f"Executor task didn't finish in time for user {user_id}, continuing anyway")
        except Exception as e:
            logger.error(f"Error waiting for executor task: {e}")

    # Cancel async task if it exists (this is the main processing task)
    task = task_info.get("task")
    if task and not task.done():
        logger.info(f"Cancelling async task for user {user_id}")
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(f"Async task cancellation timeout for user {user_id}")

    # Only remove if this is still the same job we started cancelling
    if expected_job_id:
        _remove_task_if_owner(user_id, expected_job_id)
    else:
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
                logger.info(f"Cleaned up processing tasks for user {user_id}")
