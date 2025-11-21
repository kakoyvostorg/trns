"""
Telegram Bot Route Handlers

Handles all bot commands and message types for Pyrogram with FastAPI webhook implementation.
"""

import asyncio
import logging
import os
import queue
import re
import sys
import tempfile
import threading
from typing import Optional

from pyrogram import Client
from pyrogram.types import Message, Update

from trns.bot.utils import (
    load_metadata,
    get_text,
    load_auth_key,
    is_user_authenticated,
    add_authenticated_user,
    update_context,
    reset_context,
    check_token_warning,
    check_capacity_at_start,
    get_daily_capacity,
    load_config as load_config_utils,
    save_config
)

from trns.transcription.pipeline import TranscriptionPipeline, extract_video_id
from trns.transcription.main import apply_config_to_args, create_default_config, load_config as load_config_main
from trns.bot.output_handler import send_text_to_telegram

logger = logging.getLogger(__name__)

# User states
STATE_WAITING_KEY = "waiting_key"
STATE_WAITING_CONTEXT = "waiting_context"
STATE_PROCESSING = "processing"

# Store active processing tasks per user
user_processing_tasks = {}
processing_lock = threading.Lock()

# Store user states (in-memory, can be moved to persistent storage if needed)
user_states = {}


def get_user_state(user_id: int) -> Optional[str]:
    """Get user state"""
    return user_states.get(user_id)


def set_user_state(user_id: int, state: Optional[str]):
    """Set user state"""
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


async def start_command(client: Client, message: Message) -> None:
    """Handle /start command - authentication flow"""
    from trns.bot.server import bot_metadata, bot_keyboard
    
    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()
    keyboard = bot_keyboard
    
    # Check if already authenticated
    if is_user_authenticated(user_id):
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
    from trns.bot.server import bot_metadata, bot_keyboard
    
    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()
    keyboard = bot_keyboard
    
    # Cancel any ongoing processing
    await cancel_user_processing(user_id)
    
    # Reset context
    reset_context()
    
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
    stats_text = f"📊 Статистика:\n\nОсталось запросов сегодня: {capacity} / 1000"
    
    if capacity < 50:
        stats_text += f"\n⚠️ Внимание: осталось менее 50 запросов!"
    
    await message.reply_text(stats_text)


async def cancel_user_processing(user_id: int):
    """Cancel ongoing processing for a user"""
    # Get task info outside lock to avoid holding lock during async operations
    task_info = None
    with processing_lock:
        if user_id in user_processing_tasks:
            # Make a copy of task info to work with outside the lock
            task_info = user_processing_tasks[user_id].copy()
    
    if not task_info:
        return
    
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
        # Wait a bit for the pipeline to respond to shutdown flag
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
    
    # Finally, remove from tasks dict
    with processing_lock:
        if user_id in user_processing_tasks:
            del user_processing_tasks[user_id]
            logger.info(f"Cleaned up processing tasks for user {user_id}")


def is_youtube_url(text: str) -> bool:
    """Check if text is a YouTube URL"""
    youtube_patterns = [
        r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/live/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
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


async def process_youtube_video(url: str, user_id: int, client: Client, message: Message):
    """Process YouTube video using TranscriptionPipeline with callback handler"""
    from trns.bot.server import bot_metadata
    
    metadata = bot_metadata if bot_metadata else load_metadata()
    chat_id = message.chat.id
    
    # Get shutdown flag from task info (created atomically in handler)
    shutdown_flag = None
    with processing_lock:
        if user_id in user_processing_tasks:
            shutdown_flag = user_processing_tasks[user_id].get("shutdown_flag")
    
    if shutdown_flag is None:
        logger.error(f"Shutdown flag not found for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "error_occurred") + " Internal error: missing shutdown flag.")
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
        return
    
    try:
        # Extract video ID
        video_id = extract_video_id(url)
        logger.info(f"Processing YouTube video: {video_id}")
        
        # Load config
        config = load_config_main()
        if config is None:
            config = create_default_config()
        
        # Create a simple args object from config
        class Args:
            pass
        
        args = Args()
        args = apply_config_to_args(args, config)
        args.url = url
        
        # Validate client instance
        if client is None:
            logger.error(f"Client instance is None for user_id={user_id}, chat_id={chat_id}")
            try:
                await message.reply_text(get_text(metadata, "error_occurred") + " Client instance is invalid.")
            except Exception as e:
                logger.error(f"Error sending error message: {e}")
            return
        
        # Send processing started message
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_started"))
        except Exception as e:
            logger.error(f"Error sending processing started message: {e}")
            # Continue anyway
        
        # Use a queue to send output in real-time
        # This avoids interfering with subprocess calls (yt-dlp/ffmpeg)
        output_queue = queue.Queue()
        
        def capture_print(*args, **kwargs):
            """Capture print calls and queue them for real-time sending"""
            # Build the message like print() would
            sep = kwargs.get('sep', ' ')
            end = kwargs.get('end', '\n')
            text = sep.join(str(a) for a in args) + end
            
            # Queue the output for async sending
            try:
                output_queue.put_nowait(text)
            except queue.Full:
                # Queue full, skip (shouldn't happen with unbounded queue)
                pass
            # Also log it for debugging
            logger.debug(f"Pipeline output: {text.strip()}")
        
        # Monkey-patch print for this execution
        import builtins
        original_print = builtins.print
        
        def run_pipeline():
            """Run pipeline with captured print output"""
            builtins.print = capture_print
            try:
                pipeline = TranscriptionPipeline(
                    video_id=video_id,
                    args=args,
                    shutdown_flag=lambda: shutdown_flag.is_set()
                )
                pipeline.debug_mode = False
                pipeline.run()
            except Exception as e:
                logger.exception(f"Error in pipeline: {e}")
                # Queue error message
                try:
                    output_queue.put_nowait(f"\n[ERROR] {str(e)}\n")
                except:
                    pass
            finally:
                # Restore original print
                builtins.print = original_print
                # Signal end of output
                try:
                    output_queue.put_nowait(None)  # None signals end
                except:
                    pass
        
        # Async task to consume output queue and send to Telegram in real-time
        async def send_output_task():
            """Send output from queue to Telegram in real-time"""
            buffer = ""
            last_send_time = 0
            send_interval = 2.0  # Send every 2 seconds or immediately for transcriptions
            
            while True:
                try:
                    # Get output from queue with timeout
                    try:
                        text = output_queue.get(timeout=1.0)
                    except queue.Empty:
                        # Check if shutdown requested
                        if shutdown_flag.is_set():
                            # Send any remaining buffer
                            if buffer.strip():
                                await send_text_to_telegram(client, chat_id, buffer)
                            break
                        continue
                    
                    # None signals end of output
                    if text is None:
                        # Send any remaining buffer
                        if buffer.strip():
                            await send_text_to_telegram(client, chat_id, buffer)
                        break
                    
                    # Add to buffer
                    buffer += text
                    
                    # Check if this is a transcription (contains timestamp brackets)
                    is_transcription = '[' in text and ']' in text
                    
                    # Send immediately for transcriptions, or periodically for other output
                    import time
                    current_time = time.time()
                    should_send = (
                        is_transcription or 
                        (current_time - last_send_time >= send_interval and buffer.strip())
                    )
                    
                    if should_send and buffer.strip():
                        await send_text_to_telegram(client, chat_id, buffer)
                        buffer = ""
                        last_send_time = current_time
                    
                    output_queue.task_done()
                except Exception as e:
                    logger.error(f"Error in output sender: {e}", exc_info=True)
                    # Continue processing
        
        # Start output sender task
        output_task = asyncio.create_task(send_output_task())
        
        # Run pipeline in executor
        loop = asyncio.get_event_loop()
        executor_task = None
        try:
            # Store executor future so it can be cancelled
            executor_task = loop.run_in_executor(None, run_pipeline)
            
            # Update task info with executor task atomically
            with processing_lock:
                if user_id in user_processing_tasks:
                    user_processing_tasks[user_id]["executor_task"] = executor_task
                    user_processing_tasks[user_id]["output_task"] = output_task
            
            # Wait for pipeline to complete
            await executor_task
        except asyncio.CancelledError:
            logger.info(f"Pipeline executor task cancelled for user {user_id}")
            # Cancel the executor task if possible
            if executor_task and not executor_task.done():
                executor_task.cancel()
                try:
                    await executor_task
                except asyncio.CancelledError:
                    pass
        except Exception as e:
            logger.exception(f"Error in pipeline execution: {e}")
            try:
                await client.send_message(chat_id=chat_id, text=f"{get_text(metadata, 'error_occurred')} {str(e)}")
            except Exception as e2:
                logger.error(f"Error sending error message: {e2}")
        finally:
            # Always wait for output task to finish (with timeout - increased to 30s for LM processing)
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
        
        # Only send completion message if not cancelled
        if not shutdown_flag.is_set():
            try:
                await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_complete"))
            except Exception as e:
                logger.debug(f"Error sending completion message: {e}")
        
        # Reset context after processing
        reset_context()
        
    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        # Reset context on cancel
        reset_context()
    except Exception as e:
        logger.exception(f"Error processing YouTube video: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await client.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.error(f"Error sending error message: {e2}")
        # Reset context on error
        reset_context()
    finally:
        # Always clear processing state in finally block
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]


async def process_twitter_video(url: str, user_id: int, client: Client, message: Message):
    """Process Twitter/X.com video using TranscriptionPipeline (same as YouTube)"""
    from trns.bot.server import bot_metadata
    
    metadata = bot_metadata if bot_metadata else load_metadata()
    chat_id = message.chat.id
    
    # Get shutdown flag from task info (created atomically in handler)
    shutdown_flag = None
    with processing_lock:
        if user_id in user_processing_tasks:
            shutdown_flag = user_processing_tasks[user_id].get("shutdown_flag")
    
    if shutdown_flag is None:
        logger.error(f"Shutdown flag not found for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "error_occurred") + " Internal error: missing shutdown flag.")
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
        return
    
    try:
        # For Twitter, use the URL directly as video_id (yt-dlp handles Twitter URLs)
        # Extract a simple identifier from URL for logging
        video_id = url
        if "/status/" in url:
            # Extract status ID for logging
            try:
                status_id = url.split("/status/")[-1].split("?")[0]
                video_id = f"twitter_{status_id}"
            except:
                video_id = "twitter_video"
        logger.info(f"Processing Twitter video: {url}")
        
        # Load config
        config = load_config_main()
        if config is None:
            config = create_default_config()
        
        # Create a simple args object from config
        class Args:
            pass
        
        args = Args()
        args = apply_config_to_args(args, config)
        args.url = url  # Use full URL for yt-dlp
        
        # Validate client instance
        if client is None:
            logger.error(f"Client instance is None for user_id={user_id}, chat_id={chat_id}")
            try:
                await message.reply_text(get_text(metadata, "error_occurred") + " Client instance is invalid.")
            except Exception as e:
                logger.error(f"Error sending error message: {e}")
            return
        
        # Send processing started message
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_started"))
        except Exception as e:
            logger.error(f"Error sending processing started message: {e}")
            # Continue anyway
        
        # Use a queue to send output in real-time
        output_queue = queue.Queue()
        
        def capture_print(*args, **kwargs):
            """Capture print calls and queue them for real-time sending"""
            sep = kwargs.get('sep', ' ')
            end = kwargs.get('end', '\n')
            text = sep.join(str(a) for a in args) + end
            
            try:
                output_queue.put_nowait(text)
            except queue.Full:
                pass
            logger.debug(f"Pipeline output: {text.strip()}")
        
        # Monkey-patch print for this execution
        import builtins
        original_print = builtins.print
        
        def run_pipeline():
            """Run pipeline with captured print output"""
            builtins.print = capture_print
            try:
                # For Twitter, we need to modify TranscriptionPipeline to accept URLs directly
                # Since it expects video_id, we'll pass the URL and modify the pipeline's behavior
                # Actually, yt-dlp in WhisperTranscriber should handle Twitter URLs
                # But TranscriptionPipeline uses video_id for YouTube-specific logic
                # Let's use the URL as video_id and let yt-dlp handle it
                pipeline = TranscriptionPipeline(
                    video_id=url,  # Pass URL directly, yt-dlp will handle it
                    args=args,
                    shutdown_flag=lambda: shutdown_flag.is_set()
                )
                pipeline.debug_mode = False
                pipeline.run()
            except Exception as e:
                logger.exception(f"Error in pipeline: {e}")
                try:
                    output_queue.put_nowait(f"\n[ERROR] {str(e)}\n")
                except:
                    pass
            finally:
                # Restore original print
                builtins.print = original_print
                # Signal end of output
                try:
                    output_queue.put_nowait(None)  # None signals end
                except:
                    pass
        
        # Async task to consume output queue and send to Telegram in real-time
        async def send_output_task():
            """Send output from queue to Telegram in real-time"""
            buffer = ""
            last_send_time = 0
            send_interval = 2.0  # Send every 2 seconds or immediately for transcriptions
            
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
                    
                    import time
                    current_time = time.time()
                    should_send = (
                        is_transcription or 
                        (current_time - last_send_time >= send_interval and buffer.strip())
                    )
                    
                    if should_send and buffer.strip():
                        await send_text_to_telegram(client, chat_id, buffer)
                        buffer = ""
                        last_send_time = current_time
                    
                    output_queue.task_done()
                except Exception as e:
                    logger.error(f"Error in output sender: {e}", exc_info=True)
        
        # Start output sender task
        output_task = asyncio.create_task(send_output_task())
        
        # Run pipeline in executor
        loop = asyncio.get_event_loop()
        executor_task = None
        try:
            executor_task = loop.run_in_executor(None, run_pipeline)
            
            # Update task info with executor task atomically
            with processing_lock:
                if user_id in user_processing_tasks:
                    user_processing_tasks[user_id]["executor_task"] = executor_task
                    user_processing_tasks[user_id]["output_task"] = output_task
            
            # Wait for pipeline to complete
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
                await client.send_message(chat_id=chat_id, text=f"{get_text(metadata, 'error_occurred')} {str(e)}")
            except Exception as e2:
                logger.error(f"Error sending error message: {e2}")
        finally:
            # Always wait for output task to finish (with timeout - increased to 30s for LM processing)
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
        
        # Only send completion message if not cancelled
        if not shutdown_flag.is_set():
            try:
                await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_complete"))
            except Exception as e:
                logger.debug(f"Error sending completion message: {e}")
        
        # Reset context after processing
        reset_context()
        
    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        # Reset context on cancel
        reset_context()
    except Exception as e:
        logger.exception(f"Error processing Twitter video: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await client.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.error(f"Error sending error message: {e2}")
        # Reset context on error
        reset_context()
    finally:
        # Always clear processing state in finally block
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]


async def process_video_file(video_path: str, user_id: int, client: Client, message: Message):
    """Process uploaded video file using TranscriptionPipeline with full processing"""
    from trns.bot.server import bot_metadata
    
    metadata = bot_metadata if bot_metadata else load_metadata()
    chat_id = message.chat.id
    
    # Get shutdown flag from task info (created atomically in handler)
    shutdown_flag = None
    with processing_lock:
        if user_id in user_processing_tasks:
            shutdown_flag = user_processing_tasks[user_id].get("shutdown_flag")
    
    if shutdown_flag is None:
        logger.error(f"Shutdown flag not found for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "error_occurred") + " Internal error: missing shutdown flag.")
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
        return
    
    audio_path = None
    try:
        # Extract audio from video using ffmpeg
        import subprocess
        
        temp_dir = tempfile.gettempdir()
        # Use MP3 format for faster extraction and smaller temp files
        audio_path = os.path.join(temp_dir, f"telegram_audio_{user_id}_{os.path.basename(video_path)}.mp3")
        
        # Extract audio (silently) - MP3 is much faster than uncompressed WAV
        ffmpeg_cmd = [
            "ffmpeg", "-i", video_path,
            "-vn", "-acodec", "libmp3lame",
            "-ar", "16000", "-ac", "1",
            "-b:a", "32k",
            "-y", audio_path
        ]
        
        import time
        ffmpeg_start = time.time()
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")
        ffmpeg_time = time.time() - ffmpeg_start
        logger.info(f"[PERF] Audio extraction completed in {ffmpeg_time:.1f}s")
        
        # Load config
        config = load_config_main()
        if config is None:
            config = create_default_config()
        
        # Create args object
        class Args:
            pass
        
        args = Args()
        args = apply_config_to_args(args, config)
        args.url = ""  # Not a YouTube URL
        args.process_mode = "full"  # Process entire video at once
        
        # Validate client instance
        if client is None:
            logger.error(f"Client instance is None for user_id={user_id}, chat_id={chat_id}")
            try:
                await message.reply_text(get_text(metadata, "error_occurred") + " Client instance is invalid.")
            except Exception as e:
                logger.error(f"Error sending error message: {e}")
            return
        
        # Send processing started message
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_started"))
        except Exception as e:
            logger.error(f"Error sending processing started message: {e}")
            # Continue anyway
        
        # Use a queue to send output in real-time
        output_queue = queue.Queue()
        
        def capture_print(*args, **kwargs):
            """Capture print calls and queue them for real-time sending"""
            sep = kwargs.get('sep', ' ')
            end = kwargs.get('end', '\n')
            text = sep.join(str(a) for a in args) + end
            
            try:
                output_queue.put_nowait(text)
            except queue.Full:
                pass
            logger.debug(f"Pipeline output: {text.strip()}")
        
        # Monkey-patch print for this execution
        import builtins
        original_print = builtins.print
        
        def run_pipeline():
            """Run pipeline with captured print output and local audio file"""
            builtins.print = capture_print
            try:
                from trns.transcription.whisper_transcriber import WhisperTranscriber
                from trns.transcription.language_model import LMProcessor
                from datetime import datetime
                
                # Initialize transcriber
                transcriber = WhisperTranscriber(
                    model_size=args.whisper_model,
                    use_faster_whisper=args.use_faster_whisper,
                    shutdown_flag=lambda: shutdown_flag.is_set()
                )
                
                if shutdown_flag.is_set():
                    return
                
                # Detect language (silently)
                detected_language, lang_prob = transcriber._detect_language(audio_path)
                
                if shutdown_flag.is_set():
                    return
                
                # Get transcription model
                model = transcriber._get_transcription_model(detected_language)
                
                # Transcribe (silently) - using beam_size=3 for balance between speed and accuracy
                import time
                transcription_start = time.time()
                if transcriber.use_faster_whisper:
                    segments, info = model.transcribe(audio_path, beam_size=3)
                    all_segments = []
                    for segment in segments:
                        if shutdown_flag.is_set():
                            break
                        all_segments.append(segment)
                    text = " ".join([seg.text for seg in all_segments]).strip()
                    if detected_language != "unknown":
                        final_language = detected_language
                        final_prob = lang_prob
                    else:
                        final_language = info.language if hasattr(info, 'language') else "unknown"
                        final_prob = info.language_probability if hasattr(info, 'language_probability') else 0.0
                    detected_language = final_language
                    language_prob = final_prob
                else:
                    result = model.transcribe(audio_path, verbose=False)
                    text = result["text"].strip()
                    if detected_language != "unknown":
                        final_language = detected_language
                        final_prob = lang_prob
                    else:
                        final_language = result.get("language", "unknown")
                        final_prob = 1.0
                    detected_language = final_language
                    language_prob = final_prob
                
                if shutdown_flag.is_set():
                    return
                
                transcription_time = time.time() - transcription_start
                logger.info(f"[PERF] Whisper transcription completed in {transcription_time:.1f}s")
                
                # Translate
                if text.strip():
                    if detected_language != 'ru':
                        translated_text = transcriber.translate_to_russian(text, detected_language)
                    else:
                        translated_text = text
                    
                    # Format output based on translation_output setting
                    if args.translation_output == "russian-only":
                        output_text = translated_text
                    elif args.translation_output == "both":
                        output_text = f"[{detected_language}] {text}\n[RU] {translated_text}"
                    else:
                        output_text = text
                    
                    # Output without timestamp
                    print(output_text)
                    
                    # Process through LM if enabled
                    logger.info(f"[LM] Checking LM config: has lm_output_mode={hasattr(args, 'lm_output_mode')}, mode={getattr(args, 'lm_output_mode', 'NOT SET')}")
                    if hasattr(args, 'lm_output_mode') and args.lm_output_mode != "transcriptions-only":
                        try:
                            logger.info(f"[LM] Starting LM processing with mode: {args.lm_output_mode}")
                            lm_start = time.time()
                            lm_processor = LMProcessor(
                                api_key_file=getattr(args, 'lm_api_key_file', 'api_key.txt'),
                                prompt_file=getattr(args, 'lm_prompt_file', 'prompt.md'),
                                model=getattr(args, 'lm_model', 'google/gemma-3-27b-it:free'),
                                window_seconds=getattr(args, 'lm_window_seconds', 120),
                                interval=getattr(args, 'lm_interval', 30),
                                context=getattr(args, 'context', ''),
                                shutdown_flag=lambda: shutdown_flag.is_set()
                            )
                            
                            transcription_data = [{
                                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                'text': text,
                                'translated': translated_text,
                                'iteration': 1,
                                'language': detected_language,
                                'language_prob': language_prob
                            }]
                            
                            if not shutdown_flag.is_set():
                                logger.info("[LM] Calling process_transcription_window...")
                                report = lm_processor.process_transcription_window(transcription_data)
                                logger.info(f"[LM] Report generated: {bool(report)}, length: {len(report) if report else 0}")
                                if report:
                                    # Send LM Report label as separate message
                                    logger.info(f"[LM] Sending report to user via print()")
                                    lm_label = get_text(metadata, 'lm_report_label')
                                    logger.info(f"[LM] Label text: {lm_label}")
                                    print(f"\n{lm_label}")
                                    logger.info(f"[LM] Printing report (first 100 chars): {report[:100]}...")
                                    print(report)
                                    logger.info("[LM] Report printed successfully")
                                else:
                                    logger.warning("[LM] No report generated from LM processor")
                            else:
                                logger.info("[LM] Shutdown flag set, skipping report generation")
                            lm_time = time.time() - lm_start
                            logger.info(f"[PERF] LM processing completed in {lm_time:.1f}s")
                        except Exception as e:
                            logger.exception(f"LM processing error: {e}")
                            print(f"LM processing error: {e}")
                
                # Clean up audio file
                try:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                except Exception as e:
                    logger.debug(f"Error cleaning up audio file: {e}")
                
                print("Transcription complete!")
                
            except Exception as e:
                logger.exception(f"Error in pipeline: {e}")
                try:
                    output_queue.put_nowait(f"\n[ERROR] {str(e)}\n")
                except:
                    pass
            finally:
                # Restore original print
                builtins.print = original_print
                # Signal end of output
                try:
                    output_queue.put_nowait(None)  # None signals end
                except:
                    pass
        
        # Async task to consume output queue and send to Telegram in real-time
        async def send_output_task():
            """Send output from queue to Telegram in real-time"""
            buffer = ""
            last_send_time = 0
            send_interval = 2.0  # Send every 2 seconds or immediately for transcriptions
            
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
                    
                    import time
                    current_time = time.time()
                    should_send = (
                        is_transcription or 
                        (current_time - last_send_time >= send_interval and buffer.strip())
                    )
                    
                    if should_send and buffer.strip():
                        await send_text_to_telegram(client, chat_id, buffer)
                        buffer = ""
                        last_send_time = current_time
                    
                    output_queue.task_done()
                except Exception as e:
                    logger.error(f"Error in output sender: {e}", exc_info=True)
        
        # Start output sender task
        output_task = asyncio.create_task(send_output_task())
        
        # Run pipeline in executor
        loop = asyncio.get_event_loop()
        executor_task = None
        try:
            executor_task = loop.run_in_executor(None, run_pipeline)
            
            # Update task info with executor task atomically
            with processing_lock:
                if user_id in user_processing_tasks:
                    user_processing_tasks[user_id]["executor_task"] = executor_task
                    user_processing_tasks[user_id]["output_task"] = output_task
            
            # Wait for pipeline to complete
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
                await client.send_message(chat_id=chat_id, text=f"{get_text(metadata, 'error_occurred')} {str(e)}")
            except Exception as e2:
                logger.error(f"Error sending error message: {e2}")
        finally:
            # Always wait for output task to finish (with timeout - increased to 30s for LM processing)
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
        
        # Only send completion message if not cancelled
        if not shutdown_flag.is_set():
            try:
                await client.send_message(chat_id=chat_id, text=get_text(metadata, "processing_complete"))
            except Exception as e:
                logger.debug(f"Error sending completion message: {e}")
        
        # Reset context after processing
        reset_context()
        
    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await client.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        # Reset context on cancel
        reset_context()
    except Exception as e:
        logger.exception(f"Error processing video file: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await client.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.error(f"Error sending error message: {e2}")
        # Reset context on error
        reset_context()
    finally:
        # Always clean up files and processing state in finally block
        # Clean up audio file
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception as e:
                logger.warning(f"Error cleaning up audio file: {e}")
        
        # Clean up video file
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception as e:
                logger.warning(f"Error cleaning up video file: {e}")
        
        # Clear processing state
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]


async def handle_text_message(client: Client, message: Message) -> None:
    """Handle text messages (authentication, YouTube links, button clicks, context, tokens)"""
    from trns.bot.server import bot_metadata, bot_keyboard
    
    user_id = message.from_user.id
    text = message.text
    metadata = bot_metadata if bot_metadata else load_metadata()
    keyboard = bot_keyboard
    
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
    
    # Handle button clicks
    context_btn_text = get_text(metadata, "context_button")
    cancel_btn_text = get_text(metadata, "cancel_button")
    
    if text == context_btn_text:
        await handle_context_button(client, message)
        return
    elif text == cancel_btn_text:
        await cancel_command(client, message)
        return
    
    # Handle state-based inputs
    state = get_user_state(user_id)
    
    if state == STATE_WAITING_CONTEXT:
        # User is entering context
        update_context(text)
        set_user_state(user_id, None)
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
        with processing_lock:
            if user_id in user_processing_tasks:
                # Already processing, inform user
                asyncio.create_task(
                    message.reply_text(get_text(metadata, "processing_video"))
                ).add_done_callback(lambda t: handle_task_error(t, user_id))
                return
            
            # Create task info atomically before starting processing
            user_processing_tasks[user_id] = {
                "shutdown_flag": shutdown_flag,
                "type": "youtube"
            }
        
        # Set processing state
        set_user_state(user_id, STATE_PROCESSING)
        
        # Check capacity at start of processing
        capacity, should_warn = check_capacity_at_start()
        if should_warn:
            warning_text = get_text(metadata, "token_warning")
            asyncio.create_task(
                message.reply_text(warning_text)
            ).add_done_callback(lambda t: handle_task_error(t, user_id))
        
        # Send initial processing message
        chat_id = message.chat.id
        try:
            await message.reply_text(get_text(metadata, "processing_youtube"))
            logger.info(f"Initial processing message sent successfully to chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Failed to send initial processing message to chat_id={chat_id}: {e}", exc_info=True)
            # Clean up on error
            with processing_lock:
                if user_id in user_processing_tasks:
                    del user_processing_tasks[user_id]
            set_user_state(user_id, None)
            return
        
        # Start processing in background (fire-and-forget)
        async def process_with_cleanup():
            """Process video and ensure cleanup"""
            try:
                await process_youtube_video(text, user_id, client, message)
            finally:
                # Always clear state, even on error
                set_user_state(user_id, None)
        
        task = asyncio.create_task(process_with_cleanup())
        task.add_done_callback(lambda t: handle_task_error(t, user_id))
        
        # Update task info with the async task
        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task
        
        return
    
    # Check if it's a Twitter/X.com URL
    is_tw = is_twitter_url(text)
    logger.info(f"[TEXT] Twitter URL check: {is_tw} for text: {text[:100] if text else 'None'}")
    if is_tw:
        # Check if already processing and create task info atomically
        shutdown_flag = threading.Event()
        with processing_lock:
            if user_id in user_processing_tasks:
                # Already processing, inform user
                asyncio.create_task(
                    message.reply_text(get_text(metadata, "processing_video"))
                ).add_done_callback(lambda t: handle_task_error(t, user_id))
                return
            
            # Create task info atomically before starting processing
            user_processing_tasks[user_id] = {
                "shutdown_flag": shutdown_flag,
                "type": "twitter"
            }
        
        # Set processing state
        set_user_state(user_id, STATE_PROCESSING)
        
        # Check capacity at start of processing
        capacity, should_warn = check_capacity_at_start()
        if should_warn:
            warning_text = get_text(metadata, "token_warning")
            asyncio.create_task(
                message.reply_text(warning_text)
            ).add_done_callback(lambda t: handle_task_error(t, user_id))
        
        # Send initial processing message
        chat_id = message.chat.id
        try:
            await message.reply_text(get_text(metadata, "processing_twitter"))
            logger.info(f"Initial processing message sent successfully to chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Failed to send initial processing message to chat_id={chat_id}: {e}", exc_info=True)
            # Clean up on error
            with processing_lock:
                if user_id in user_processing_tasks:
                    del user_processing_tasks[user_id]
            set_user_state(user_id, None)
            return
        
        # Start processing in background (fire-and-forget)
        async def process_with_cleanup():
            """Process video and ensure cleanup"""
            try:
                await process_twitter_video(text, user_id, client, message)
            finally:
                # Always clear state, even on error
                set_user_state(user_id, None)
        
        task = asyncio.create_task(process_with_cleanup())
        task.add_done_callback(lambda t: handle_task_error(t, user_id))
        
        # Update task info with the async task
        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task
        
        return
    
    # Unknown text
    await message.reply_text(
        get_text(metadata, "unknown_text"),
        reply_markup=keyboard
    )


async def handle_video_message(client: Client, message: Message) -> None:
    """Handle video file uploads - now supports up to 2GB with Pyrogram"""
    from trns.bot.server import bot_metadata, bot_keyboard
    
    user_id = message.from_user.id
    metadata = bot_metadata if bot_metadata else load_metadata()
    keyboard = bot_keyboard
    
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
    MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB in bytes
    
    if file_size > MAX_FILE_SIZE:
        await message.reply_text(
            f"❌ File is too large ({file_size / (1024*1024*1024):.2f} GB). "
            f"Maximum size is {MAX_FILE_SIZE / (1024*1024*1024):.0f} GB."
        )
        return
    
    # Check token warning
    if check_token_warning():
        warning_text = get_text(metadata, "token_warning")
        await message.reply_text(warning_text)
    
    # Set processing state
    set_user_state(user_id, STATE_PROCESSING)
    
    # Create shutdown flag and task info atomically BEFORE any I/O operations
    # This prevents race condition where two concurrent requests both pass the check
    shutdown_flag = threading.Event()
    with processing_lock:
        if user_id in user_processing_tasks:
            # Already processing, inform user
            asyncio.create_task(
                message.reply_text(get_text(metadata, "processing_video"))
            ).add_done_callback(lambda t: handle_task_error(t, user_id))
            set_user_state(user_id, None)
            return
        
        # Create task info atomically before starting processing
        user_processing_tasks[user_id] = {
            "shutdown_flag": shutdown_flag,
            "type": "file"
        }
    
    # Now safe to do I/O operations - user is marked as processing
    await message.reply_text(get_text(metadata, "downloading_video"))
    
    # Download video file using Pyrogram
    video_path = None
    try:
        temp_dir = tempfile.gettempdir()
        # Determine file extension
        if hasattr(video, 'file_name') and video.file_name:
            file_ext = video.file_name.split('.')[-1]
        elif message.video:
            file_ext = 'mp4'
        else:
            file_ext = 'mp4'
        
        video_path = os.path.join(temp_dir, f"telegram_video_{user_id}_{video.file_id}.{file_ext}")
        
        # Pyrogram download - much simpler!
        import time
        download_start = time.time()
        await message.download(file_name=video_path)
        download_time = time.time() - download_start
        logger.info(f"[PERF] Video download completed in {download_time:.1f}s ({file_size / (1024*1024):.1f}MB)")
        
        # Start processing in background (fire-and-forget)
        async def process_with_cleanup():
            """Process video and ensure cleanup"""
            try:
                await process_video_file(video_path, user_id, client, message)
            finally:
                # Always clear state, even on error
                set_user_state(user_id, None)
        
        task = asyncio.create_task(process_with_cleanup())
        task.add_done_callback(lambda t: handle_task_error(t, user_id))
        
        # Update task info with the async task
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
        # Clean up on error
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
        set_user_state(user_id, None)
        # Clean up downloaded file if it exists
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


async def route_update(client: Client, update: Update):
    """Route Pyrogram updates to appropriate handlers"""
    try:
        # Check if update has a message
        if not update.message:
            logger.debug("Update has no message, ignoring")
            return
        
        message = update.message
        
        # Handle commands
        if message.text and message.text.startswith('/'):
            command = message.text.split()[0].lower()
            
            if command == '/start':
                await start_command(client, message)
            elif command == '/cancel':
                await cancel_command(client, message)
            elif command == '/stats':
                await stats_command(client, message)
            else:
                # Unknown command
                logger.debug(f"Unknown command: {command}")
            return
        
        # Handle text messages
        if message.text:
            await handle_text_message(client, message)
            return
        
        # Handle video messages
        if message.video or (message.document and message.document.mime_type and 'video' in message.document.mime_type):
            await handle_video_message(client, message)
            return
        
        # If we get here, it's an unsupported message type
        logger.debug(f"Unsupported message type from user {message.from_user.id}")
        
    except Exception as e:
        logger.exception(f"Error routing update: {e}")
