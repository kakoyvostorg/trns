"""URL and file transcription processing for the Telegram bot."""
import asyncio
import logging
import os
import queue
import re
import threading

from pyrogram import Client
from pyrogram.types import Message

from trns.bot.utils import (
    load_metadata,
    get_text,
    get_user_setting,
    reset_context,
    get_context,
)
from trns.transcription.pipeline import TranscriptionPipeline, extract_video_id
from trns.transcription.main import apply_config_to_args, create_default_config, load_config as load_config_main
from trns.bot.output_handler import send_text_to_telegram

from ._state import (
    _resolve_args_paths,
    processing_lock,
    user_processing_tasks,
    _remove_task_if_owner,
)

logger = logging.getLogger(__name__)


def is_youtube_url(text: str) -> bool:
    """Check if text is a YouTube URL"""
    youtube_patterns = [
        r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/live/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in youtube_patterns:
        if re.search(pattern, text):
            return True
    return False


def is_twitter_url(text: str) -> bool:
    """Check if text is a Twitter/X.com URL"""
    twitter_patterns = [
        r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+',
        r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/\w+/statuses/\d+',
        r'(?:https?://)?t\.co/[a-zA-Z0-9]+',  # Shortened Twitter links
    ]
    for pattern in twitter_patterns:
        if re.search(pattern, text):
            return True
    return False


async def _run_pipeline_with_output(
    video_id: str, args, user_id: int, client: Client,
    chat_id: int, shutdown_flag: threading.Event, metadata: dict, source: str
):
    """
    Shared helper: run TranscriptionPipeline in an executor thread and stream
    output to Telegram via a queue-based async consumer.

    Replaces the old builtins.print monkey-patch with a thread-safe
    output_callback passed directly to the pipeline.
    """
    output_queue = queue.Queue()

    def output_callback(text: str):
        """Thread-safe callback that queues text for async sending."""
        try:
            output_queue.put_nowait(text)
        except queue.Full:
            pass
        logger.debug(f"Pipeline output: {text.strip()}")

    def run_pipeline():
        """Run pipeline in executor thread (no builtins.print patching)."""
        try:
            pipeline = TranscriptionPipeline(
                video_id=video_id,
                args=args,
                shutdown_flag=lambda: shutdown_flag.is_set(),
                output_callback=output_callback,
            )
            pipeline.debug_mode = False
            pipeline.run()
        except Exception as e:
            logger.exception(f"Error in pipeline: {e}")
            try:
                output_queue.put_nowait(f"\n[ERROR] {str(e)}\n")
            except queue.Full:
                logger.debug("Output queue full, could not enqueue error line")
            except Exception as ex:
                logger.debug("Could not enqueue error line: %s", ex)
        finally:
            try:
                output_queue.put_nowait(None)  # None signals end
            except queue.Full:
                logger.debug("Output queue full, could not enqueue sentinel")
            except Exception as ex:
                logger.debug("Could not enqueue sentinel: %s", ex)

    async def send_output_task():
        """Consume the output queue and send to Telegram in real-time."""
        import time as _time
        buffer = ""
        last_send_time = 0
        send_interval = 2.0

        while True:
            try:
                try:
                    text = output_queue.get(timeout=1.0)
                except queue.Empty:
                    if shutdown_flag.is_set():
                        if buffer.strip():
                            await send_text_to_telegram(client, chat_id, buffer)
                        break
                    continue

                if text is None:
                    if buffer.strip():
                        await send_text_to_telegram(client, chat_id, buffer)
                    break

                buffer += text
                is_transcription = '[' in text and ']' in text
                current_time = _time.time()
                should_send = (
                    is_transcription
                    or (current_time - last_send_time >= send_interval and buffer.strip())
                )

                if should_send and buffer.strip():
                    await send_text_to_telegram(client, chat_id, buffer)
                    buffer = ""
                    last_send_time = current_time

                output_queue.task_done()
            except Exception as e:
                logger.error(f"Error in output sender: {e}", exc_info=True)

    # Send processing started message
    try:
        await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_started"))
    except Exception as e:
        logger.error(f"Error sending processing started message: {e}")

    output_task = asyncio.create_task(send_output_task())

    loop = asyncio.get_event_loop()
    executor_task = None
    try:
        executor_task = loop.run_in_executor(None, run_pipeline)

        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["executor_task"] = executor_task
                user_processing_tasks[user_id]["output_task"] = output_task

        await executor_task
    except asyncio.CancelledError:
        logger.info(f"Pipeline executor task cancelled for user {user_id}")
        if executor_task and not executor_task.done():
            executor_task.cancel()
            try:
                await executor_task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        logger.exception(f"Error in pipeline execution: {e}")
        try:
            await client.send_message(
                chat_id=chat_id,
                text=f"{get_text(metadata, 'error_occurred')} {str(e)}",
            )
        except Exception as e2:
            logger.error(f"Error sending error message: {e2}")
    finally:
        try:
            await asyncio.wait_for(output_task, timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for output task, cancelling")
            output_task.cancel()
            try:
                await asyncio.wait_for(output_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        except asyncio.CancelledError:
            pass

    if not shutdown_flag.is_set():
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_complete"))
        except Exception as e:
            logger.debug(f"Error sending completion message: {e}")


async def _process_url_video(url: str, video_id: str, user_id: int, client: Client, message: Message, source: str, job_id: str = ""):
    """
    Shared processing for YouTube and Twitter/X.com URLs.
    Handles config loading, user settings, pipeline execution, and cleanup.
    """
    from trns.bot.server import bot_metadata

    metadata = bot_metadata if bot_metadata else load_metadata()
    chat_id = message.chat.id

    shutdown_flag = None
    with processing_lock:
        if user_id in user_processing_tasks:
            shutdown_flag = user_processing_tasks[user_id].get("shutdown_flag")

    if shutdown_flag is None:
        logger.error(f"Shutdown flag not found for user {user_id}")
        try:
            await client.send_message(
                chat_id=chat_id,
                text=get_text(metadata, "error_occurred") + " Internal error: missing shutdown flag.",
            )
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
        return

    try:
        logger.info(f"Processing {source} video: {video_id}")

        config = load_config_main()
        if config is None:
            config = create_default_config()

        # Load user settings (fixes bug where YouTube path was missing this)
        show_original = get_user_setting(user_id, "show_original_translation", default=True)
        show_transcription = get_user_setting(user_id, "show_transcription", default=True)

        class Args:
            pass

        args = Args()
        args = apply_config_to_args(args, config)
        args = _resolve_args_paths(args)
        args.url = url
        args.context = get_context(user_id)
        args.show_original_translation = show_original
        args.show_transcription = show_transcription

        if client is None:
            logger.error(f"Client instance is None for user_id={user_id}, chat_id={chat_id}")
            try:
                await message.reply_text(get_text(metadata, "error_occurred") + " Client instance is invalid.")
            except Exception as e:
                logger.error(f"Error sending error message: {e}")
            return

        await _run_pipeline_with_output(
            video_id=video_id, args=args, user_id=user_id,
            client=client, chat_id=chat_id, shutdown_flag=shutdown_flag,
            metadata=metadata, source=source,
        )

        reset_context(user_id)

    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        reset_context(user_id)
    except Exception as e:
        logger.exception(f"Error processing {source} video: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await client.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.error(f"Error sending error message: {e2}")
        reset_context(user_id)
    finally:
        if job_id:
            _remove_task_if_owner(user_id, job_id)
        else:
            with processing_lock:
                if user_id in user_processing_tasks:
                    del user_processing_tasks[user_id]


async def process_youtube_video(url: str, user_id: int, client: Client, message: Message, job_id: str = ""):
    """Process YouTube video using TranscriptionPipeline."""
    video_id = extract_video_id(url)
    await _process_url_video(url, video_id, user_id, client, message, source="YouTube", job_id=job_id)


async def process_twitter_video(url: str, user_id: int, client: Client, message: Message, job_id: str = ""):
    """Process Twitter/X.com video using TranscriptionPipeline.

    Pass the full URL as video_id so yt-dlp can fetch it directly.
    The synthetic twitter_ prefix was causing the transcription layer
    to treat it as a YouTube video ID.
    """
    await _process_url_video(url, url, user_id, client, message, source="Twitter", job_id=job_id)


async def process_video_file(video_path: str, user_id: int, client: Client, message: Message, job_id: str = ""):
    """Process uploaded video file via TranscriptionPipeline (same orchestration as URL jobs)."""
    from trns.bot.server import bot_metadata

    metadata = bot_metadata if bot_metadata else load_metadata()
    chat_id = message.chat.id

    shutdown_flag = None
    with processing_lock:
        if user_id in user_processing_tasks:
            shutdown_flag = user_processing_tasks[user_id].get("shutdown_flag")

    if shutdown_flag is None:
        logger.error(f"Shutdown flag not found for user {user_id}")
        try:
            await client.send_message(
                chat_id=chat_id,
                text=get_text(metadata, "error_occurred") + " Internal error: missing shutdown flag.",
            )
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
        return

    try:
        logger.info(f"Processing uploaded video file: {video_path}")

        config = load_config_main()
        if config is None:
            config = create_default_config()

        show_original = get_user_setting(user_id, "show_original_translation", default=True)
        show_transcription = get_user_setting(user_id, "show_transcription", default=True)

        class Args:
            pass

        args = Args()
        args = apply_config_to_args(args, config)
        args = _resolve_args_paths(args)
        args.url = video_path
        args.process_mode = "full"
        args.method = "whisper"
        args.context = get_context(user_id)
        args.show_original_translation = show_original
        args.show_transcription = show_transcription

        if client is None:
            logger.error(f"Client instance is None for user_id={user_id}, chat_id={chat_id}")
            try:
                await message.reply_text(get_text(metadata, "error_occurred") + " Client instance is invalid.")
            except Exception as e:
                logger.error(f"Error sending error message: {e}")
            return

        await _run_pipeline_with_output(
            video_id=video_path,
            args=args,
            user_id=user_id,
            client=client,
            chat_id=chat_id,
            shutdown_flag=shutdown_flag,
            metadata=metadata,
            source="file",
        )

        reset_context(user_id)

    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        reset_context(user_id)
    except Exception as e:
        logger.exception(f"Error processing video file: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await client.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.error(f"Error sending error message: {e2}")
        reset_context(user_id)
    finally:
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception as e:
                logger.warning(f"Error cleaning up video file: {e}")

        if job_id:
            _remove_task_if_owner(user_id, job_id)
        else:
            with processing_lock:
                if user_id in user_processing_tasks:
                    del user_processing_tasks[user_id]
