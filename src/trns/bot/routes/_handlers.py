"""Inbound message and callback handlers for the Telegram bot."""
import asyncio
import logging
import os
import tempfile
import threading
import uuid

from pyrogram import Client
from pyrogram.types import Message

from trns.bot.utils import (
    add_authenticated_user,
    check_capacity_at_start,
    check_token_warning,
    get_text,
    get_user_setting,
    initialize_user_settings,
    is_user_authenticated,
    load_auth_key,
    load_metadata,
    set_user_setting,
    update_context,
)

from ._commands import cancel_command
from ._processing import (
    is_twitter_url,
    is_youtube_url,
    process_twitter_video,
    process_video_file,
    process_youtube_video,
)
from ._state import (
    STATE_PROCESSING,
    STATE_WAITING_CONTEXT,
    STATE_WAITING_KEY,
    _remove_task_if_owner,
    get_user_state,
    handle_task_error,
    processing_lock,
    set_user_state,
    user_processing_tasks,
)

logger = logging.getLogger(__name__)


async def handle_text_message(client: Client, message: Message) -> None:
    """Handle text messages (authentication, YouTube links, button clicks, context, tokens)"""
    from trns.bot.server import bot_metadata, create_keyboard

    user_id = message.from_user.id
    text = message.text
    metadata = bot_metadata if bot_metadata else load_metadata()

    logger.info(f"[TEXT] Received text message from user {user_id}: {text[:100] if text else 'None'}...")

    # Check authentication (except for /cancel which is handled separately)
    if not is_user_authenticated(user_id):
        state = get_user_state(user_id)
        if state == STATE_WAITING_KEY:
            # Check authentication key
            try:
                auth_key = load_auth_key()
                if text.strip() == auth_key:
                    add_authenticated_user(user_id)
                    set_user_state(user_id, None)

                    # Initialize user settings
                    initialize_user_settings(user_id)

                    # Create personalized keyboard
                    keyboard = create_keyboard(metadata, user_id)

                    await message.reply_text(
                        get_text(metadata, "auth_success"),
                        reply_markup=keyboard
                    )
                else:
                    await message.reply_text(get_text(metadata, "invalid_key"))
            except Exception as e:
                logger.error(f"Error checking auth key: {e}")
                await message.reply_text(get_text(metadata, "error_occurred") + f" {str(e)}")
        else:
            await message.reply_text(get_text(metadata, "not_authenticated"))
        return

    # Get button texts from metadata (need to check both states of toggle buttons)
    context_btn_text = get_text(metadata, "context_button")
    cancel_btn_text = get_text(metadata, "cancel_button")
    show_original_btn = get_text(metadata, "show_original_translation_button")
    hide_original_btn = get_text(metadata, "hide_original_translation_button")
    show_transcription_btn = get_text(metadata, "show_transcription_button")
    hide_transcription_btn = get_text(metadata, "hide_transcription_button")

    # Handle button clicks
    if text == context_btn_text:
        await handle_context_button(client, message)
        return
    elif text == cancel_btn_text:
        await cancel_command(client, message)
        return
    elif text == show_original_btn or text == hide_original_btn:
        await handle_toggle_original_button(client, message)
        return
    elif text == show_transcription_btn or text == hide_transcription_btn:
        await handle_toggle_transcription_button(client, message)
        return

    # Handle state-based inputs
    state = get_user_state(user_id)

    if state == STATE_WAITING_CONTEXT:
        # User is entering context
        update_context(user_id, text)
        set_user_state(user_id, None)
        keyboard = create_keyboard(metadata, user_id)
        await message.reply_text(
            get_text(metadata, "context_set"),
            reply_markup=keyboard
        )
        return

    # Check if it's a YouTube URL
    is_yt = is_youtube_url(text)
    logger.info(f"[TEXT] YouTube URL check: {is_yt} for text: {text[:100] if text else 'None'}")
    if is_yt:
        # Check if already processing and create task info atomically
        shutdown_flag = threading.Event()
        job_id = uuid.uuid4().hex
        with processing_lock:
            if user_id in user_processing_tasks:
                asyncio.create_task(
                    message.reply_text(get_text(metadata, "processing_video"))
                ).add_done_callback(lambda t: handle_task_error(t, user_id))
                return

            user_processing_tasks[user_id] = {
                "shutdown_flag": shutdown_flag,
                "type": "youtube",
                "job_id": job_id,
            }

        set_user_state(user_id, STATE_PROCESSING)

        capacity, should_warn = check_capacity_at_start()
        if should_warn:
            warning_text = get_text(metadata, "token_warning")
            asyncio.create_task(
                message.reply_text(warning_text)
            ).add_done_callback(lambda t: handle_task_error(t, user_id))

        chat_id = message.chat.id
        try:
            await message.reply_text(get_text(metadata, "processing_youtube"))
            logger.info(f"Initial processing message sent successfully to chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Failed to send initial processing message to chat_id={chat_id}: {e}", exc_info=True)
            _remove_task_if_owner(user_id, job_id)
            set_user_state(user_id, None)
            return

        _yt_job_id = job_id  # capture for closure
        async def process_with_cleanup():
            """Process video and ensure cleanup"""
            try:
                await process_youtube_video(text, user_id, client, message, job_id=_yt_job_id)
            finally:
                set_user_state(user_id, None)

        task = asyncio.create_task(process_with_cleanup())
        task.add_done_callback(lambda t: handle_task_error(t, user_id))

        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task

        return

    # Check if it's a Twitter/X.com URL
    is_tw = is_twitter_url(text)
    logger.info(f"[TEXT] Twitter URL check: {is_tw} for text: {text[:100] if text else 'None'}")
    if is_tw:
        shutdown_flag = threading.Event()
        job_id = uuid.uuid4().hex
        with processing_lock:
            if user_id in user_processing_tasks:
                asyncio.create_task(
                    message.reply_text(get_text(metadata, "processing_video"))
                ).add_done_callback(lambda t: handle_task_error(t, user_id))
                return

            user_processing_tasks[user_id] = {
                "shutdown_flag": shutdown_flag,
                "type": "twitter",
                "job_id": job_id,
            }

        set_user_state(user_id, STATE_PROCESSING)

        capacity, should_warn = check_capacity_at_start()
        if should_warn:
            warning_text = get_text(metadata, "token_warning")
            asyncio.create_task(
                message.reply_text(warning_text)
            ).add_done_callback(lambda t: handle_task_error(t, user_id))

        chat_id = message.chat.id
        try:
            await message.reply_text(get_text(metadata, "processing_twitter"))
            logger.info(f"Initial processing message sent successfully to chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Failed to send initial processing message to chat_id={chat_id}: {e}", exc_info=True)
            _remove_task_if_owner(user_id, job_id)
            set_user_state(user_id, None)
            return

        _tw_job_id = job_id
        async def process_with_cleanup():
            """Process video and ensure cleanup"""
            try:
                await process_twitter_video(text, user_id, client, message, job_id=_tw_job_id)
            finally:
                set_user_state(user_id, None)

        task = asyncio.create_task(process_with_cleanup())
        task.add_done_callback(lambda t: handle_task_error(t, user_id))

        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task

        return

    # Unknown text
    keyboard = create_keyboard(metadata, user_id)
    await message.reply_text(
        get_text(metadata, "unknown_text"),
        reply_markup=keyboard
    )


async def handle_video_message(client: Client, message: Message) -> None:
    """Handle video file uploads - now supports up to 2GB with Pyrogram"""
    from trns.bot.server import bot_metadata, create_keyboard

    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()
    keyboard = create_keyboard(metadata, user_id)

    # Check authentication
    if not is_user_authenticated(user_id):
        await message.reply_text(get_text(metadata, "not_authenticated"))
        return

    # Get video file first (quick check, no I/O)
    video = message.video or message.document
    if not video:
        await message.reply_text(get_text(metadata, "no_video_file"))
        return

    # Check file size - Pyrogram supports up to 2GB
    file_size = video.file_size
    max_file_size = 2 * 1024 * 1024 * 1024  # 2GB in bytes

    if file_size is not None and file_size > max_file_size:
        await message.reply_text(
            f"❌ File is too large ({file_size / (1024*1024*1024):.2f} GB). "
            f"Maximum size is {max_file_size / (1024*1024*1024):.0f} GB."
        )
        return

    # Check token warning
    if check_token_warning():
        warning_text = get_text(metadata, "token_warning")
        await message.reply_text(warning_text)

    # Set processing state
    set_user_state(user_id, STATE_PROCESSING)

    shutdown_flag = threading.Event()
    job_id = uuid.uuid4().hex
    with processing_lock:
        if user_id in user_processing_tasks:
            asyncio.create_task(
                message.reply_text(get_text(metadata, "processing_video"))
            ).add_done_callback(lambda t: handle_task_error(t, user_id))
            set_user_state(user_id, None)
            return

        user_processing_tasks[user_id] = {
            "shutdown_flag": shutdown_flag,
            "type": "file",
            "job_id": job_id,
        }

    await message.reply_text(get_text(metadata, "downloading_video"))

    video_path = None
    try:
        temp_dir = tempfile.gettempdir()
        if hasattr(video, 'file_name') and video.file_name:
            file_ext = video.file_name.split('.')[-1]
        elif message.video:
            file_ext = 'mp4'
        else:
            file_ext = 'mp4'

        video_path = os.path.join(temp_dir, f"telegram_video_{user_id}_{video.file_id}.{file_ext}")

        import time
        download_start = time.time()
        await message.download(file_name=video_path)
        download_time = time.time() - download_start
        size_mb = f"{file_size / (1024*1024):.1f}MB" if file_size is not None else "unknown size"
        logger.info(f"[PERF] Video download completed in {download_time:.1f}s ({size_mb})")

        _file_job_id = job_id
        async def process_with_cleanup():
            """Process video and ensure cleanup"""
            try:
                await process_video_file(video_path, user_id, client, message, job_id=_file_job_id)
            finally:
                set_user_state(user_id, None)

        task = asyncio.create_task(process_with_cleanup())
        task.add_done_callback(lambda t: handle_task_error(t, user_id))

        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task

    except Exception as e:
        logger.exception(f"Error handling video: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await message.reply_text(f"{error_text} {str(e)}", reply_markup=keyboard)
        except Exception as e2:
            logger.error(f"Error sending error message: {e2}")
        _remove_task_if_owner(user_id, job_id)
        set_user_state(user_id, None)
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception as e3:
                logger.warning(f"Error cleaning up video file: {e3}")


async def handle_context_button(client: Client, message: Message) -> None:
    """Handle context button click"""
    from trns.bot.server import bot_metadata

    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()

    set_user_state(user_id, STATE_WAITING_CONTEXT)
    await message.reply_text(get_text(metadata, "enter_context"))


async def handle_toggle_original_button(client: Client, message: Message) -> None:
    """Handle toggle original translation button click"""
    from trns.bot.server import bot_metadata, create_keyboard

    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()

    # Get current setting and toggle it
    current_setting = get_user_setting(user_id, "show_original_translation", default=True)
    new_setting = not current_setting
    set_user_setting(user_id, "show_original_translation", new_setting)

    # Send confirmation with updated keyboard
    if new_setting:
        confirmation = get_text(metadata, "original_translation_enabled")
    else:
        confirmation = get_text(metadata, "original_translation_disabled")

    keyboard = create_keyboard(metadata, user_id)
    await message.reply_text(confirmation, reply_markup=keyboard)


async def handle_toggle_transcription_button(client: Client, message: Message) -> None:
    """Handle toggle transcription button click"""
    from trns.bot.server import bot_metadata, create_keyboard

    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()

    # Get current setting and toggle it
    current_setting = get_user_setting(user_id, "show_transcription", default=True)
    new_setting = not current_setting
    set_user_setting(user_id, "show_transcription", new_setting)

    # Send confirmation with updated keyboard
    if new_setting:
        confirmation = get_text(metadata, "transcription_enabled")
    else:
        confirmation = get_text(metadata, "transcription_disabled")

    keyboard = create_keyboard(metadata, user_id)
    await message.reply_text(confirmation, reply_markup=keyboard)
