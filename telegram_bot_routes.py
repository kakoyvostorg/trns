"""
Telegram Bot Route Handlers

Handles all bot commands and message types for the FastAPI webhook implementation.
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

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram_bot_utils import (
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

from youtube_live_transcription.pipeline import TranscriptionPipeline, extract_video_id
from youtube_live_transcription.main import apply_config_to_args, create_default_config, load_config as load_config_main
from telegram_output_handler import send_text_to_telegram

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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command - authentication flow"""
    user_id = update.effective_user.id
    metadata = context.bot_data.get("metadata", load_metadata())
    keyboard = context.bot_data.get("keyboard")
    
    # Check if already authenticated
    if is_user_authenticated(user_id):
        await update.message.reply_text(
            get_text(metadata, "auth_success"),
            reply_markup=keyboard
        )
        return
    
    # Start authentication flow
    set_user_state(user_id, STATE_WAITING_KEY)
    await update.message.reply_text(get_text(metadata, "start_message"))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command"""
    user_id = update.effective_user.id
    metadata = context.bot_data.get("metadata", load_metadata())
    keyboard = context.bot_data.get("keyboard")
    
    # Cancel any ongoing processing
    await cancel_user_processing(user_id)
    
    # Reset context
    reset_context()
    
    # Clear user state
    set_user_state(user_id, None)
    
    await update.message.reply_text(
        get_text(metadata, "cancel_success"),
        reply_markup=keyboard
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats command - show remaining daily capacity"""
    user_id = update.effective_user.id
    metadata = context.bot_data.get("metadata", load_metadata())
    
    # Check authentication
    if not is_user_authenticated(user_id):
        await update.message.reply_text(get_text(metadata, "not_authenticated"))
        return
    
    # Get current capacity
    capacity = get_daily_capacity()
    
    # Format message
    stats_text = f"📊 Статистика:\n\nОсталось запросов сегодня: {capacity} / 1000"
    
    if capacity < 50:
        stats_text += f"\n⚠️ Внимание: осталось менее 50 запросов!"
    
    await update.message.reply_text(stats_text)


async def cancel_user_processing(user_id: int):
    """Cancel ongoing processing for a user"""
    with processing_lock:
        if user_id in user_processing_tasks:
            task_info = user_processing_tasks[user_id]
            
            # Set shutdown flag first to stop pipeline
            if "shutdown_flag" in task_info:
                shutdown_flag = task_info["shutdown_flag"]
                if shutdown_flag:
                    shutdown_flag.set()
                    logger.info(f"Shutdown flag set for user {user_id}")
            
            # Note: Executor tasks (threads) can't be cancelled directly
            # We rely on the shutdown_flag to stop the pipeline
            # The pipeline checks shutdown_flag regularly and will stop when it's set
            if "executor_task" in task_info:
                executor_task = task_info["executor_task"]
                if executor_task and not executor_task.done():
                    logger.info(f"Shutdown flag set, waiting for executor task to finish for user {user_id}")
                    # Wait a bit for the pipeline to respond to shutdown flag
                    try:
                        await asyncio.wait_for(executor_task, timeout=10.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"Executor task didn't finish in time for user {user_id}, continuing anyway")
                    except Exception as e:
                        logger.error(f"Error waiting for executor task: {e}")
            
            # Cancel async task if it exists
            if "task" in task_info:
                task = task_info["task"]
                if task and not task.done():
                    logger.info(f"Cancelling async task for user {user_id}")
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            
            # Cancel output task if it exists
            if "output_task" in task_info:
                output_task = task_info["output_task"]
                if output_task and not output_task.done():
                    logger.info(f"Cancelling output task for user {user_id}")
                    output_task.cancel()
                    try:
                        await output_task
                    except asyncio.CancelledError:
                        pass
            
            del user_processing_tasks[user_id]
            logger.info(f"Cleaned up processing tasks for user {user_id}")


def is_youtube_url(text: str) -> bool:
    """Check if text is a YouTube URL"""
    youtube_patterns = [
        r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
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


async def process_youtube_video(url: str, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process YouTube video using TranscriptionPipeline with callback handler"""
    metadata = context.bot_data.get("metadata", load_metadata())
    bot = context.bot
    chat_id = update.effective_chat.id
    
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
        
        # Create shutdown flag
        shutdown_flag = threading.Event()
        
        # Store shutdown flag for cancellation (will be updated with tasks later)
        with processing_lock:
            if user_id not in user_processing_tasks:
                user_processing_tasks[user_id] = {}
            user_processing_tasks[user_id]["shutdown_flag"] = shutdown_flag
            user_processing_tasks[user_id]["type"] = "youtube"
        
        # Validate bot instance
        if bot is None:
            logger.error(f"Bot instance is None for user_id={user_id}, chat_id={chat_id}")
            await update.message.reply_text(get_text(metadata, "error_occurred") + " Bot instance is invalid.")
            return
        
        # Send processing started message
        await bot.send_message(chat_id=chat_id, text=get_text(metadata, "processing_started"))
        
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
                                await send_text_to_telegram(bot, chat_id, buffer)
                            break
                        continue
                    
                    # None signals end of output
                    if text is None:
                        # Send any remaining buffer
                        if buffer.strip():
                            await send_text_to_telegram(bot, chat_id, buffer)
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
                        await send_text_to_telegram(bot, chat_id, buffer)
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
            
            # Update task info with executor task
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
        except Exception as e:
            logger.exception(f"Error in pipeline execution: {e}")
            await bot.send_message(chat_id=chat_id, text=f"{get_text(metadata, 'error_occurred')} {str(e)}")
        finally:
            # Wait for output task to finish (with timeout)
            try:
                await asyncio.wait_for(output_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for output task")
                output_task.cancel()
                try:
                    await output_task
                except asyncio.CancelledError:
                    pass
        
        # Only send completion message if not cancelled
        if not shutdown_flag.is_set():
            try:
                await bot.send_message(chat_id=chat_id, text=get_text(metadata, "processing_complete"))
            except Exception as e:
                logger.debug(f"Error sending completion message: {e}")
        
        # Reset context after processing
        reset_context()
        
        # Clear processing state
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
        
    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await bot.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        # Reset context on cancel
        reset_context()
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
    except Exception as e:
        logger.exception(f"Error processing YouTube video: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await bot.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.debug(f"Error sending error message: {e2}")
        # Reset context on error
        reset_context()
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]


async def process_twitter_video(url: str, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process Twitter/X.com video using TranscriptionPipeline (same as YouTube)"""
    metadata = context.bot_data.get("metadata", load_metadata())
    bot = context.bot
    chat_id = update.effective_chat.id
    
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
        
        # Create shutdown flag
        shutdown_flag = threading.Event()
        
        # Store shutdown flag for cancellation (will be updated with tasks later)
        with processing_lock:
            if user_id not in user_processing_tasks:
                user_processing_tasks[user_id] = {}
            user_processing_tasks[user_id]["shutdown_flag"] = shutdown_flag
            user_processing_tasks[user_id]["type"] = "twitter"
        
        # Validate bot instance
        if bot is None:
            logger.error(f"Bot instance is None for user_id={user_id}, chat_id={chat_id}")
            await update.message.reply_text(get_text(metadata, "error_occurred") + " Bot instance is invalid.")
            return
        
        # Send processing started message
        await bot.send_message(chat_id=chat_id, text=get_text(metadata, "processing_started"))
        
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
                                await send_text_to_telegram(bot, chat_id, buffer)
                            break
                        continue
                    
                    if text is None:
                        if buffer.strip():
                            await send_text_to_telegram(bot, chat_id, buffer)
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
                        await send_text_to_telegram(bot, chat_id, buffer)
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
            
            # Update task info with executor task
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
        except Exception as e:
            logger.exception(f"Error in pipeline execution: {e}")
            await bot.send_message(chat_id=chat_id, text=f"{get_text(metadata, 'error_occurred')} {str(e)}")
        finally:
            # Wait for output task to finish (with timeout)
            try:
                await asyncio.wait_for(output_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for output task")
                output_task.cancel()
                try:
                    await output_task
                except asyncio.CancelledError:
                    pass
        
        # Only send completion message if not cancelled
        if not shutdown_flag.is_set():
            try:
                await bot.send_message(chat_id=chat_id, text=get_text(metadata, "processing_complete"))
            except Exception as e:
                logger.debug(f"Error sending completion message: {e}")
        
        # Reset context after processing
        reset_context()
        
        # Clear processing state
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
        
    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await bot.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        # Reset context on cancel
        reset_context()
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
    except Exception as e:
        logger.exception(f"Error processing Twitter video: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await bot.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.debug(f"Error sending error message: {e2}")
        # Reset context on error
        reset_context()
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]


async def process_video_file(video_path: str, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process uploaded video file using TranscriptionPipeline with full processing"""
    metadata = context.bot_data.get("metadata", load_metadata())
    bot = context.bot
    chat_id = update.effective_chat.id
    
    try:
        # Extract audio from video using ffmpeg
        import subprocess
        
        temp_dir = tempfile.gettempdir()
        audio_path = os.path.join(temp_dir, f"telegram_audio_{user_id}_{os.path.basename(video_path)}.wav")
        
        # Extract audio (silently)
        ffmpeg_cmd = [
            "ffmpeg", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            "-y", audio_path
        ]
        
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")
        
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
        
        # Create shutdown flag
        shutdown_flag = threading.Event()
        
        # Store shutdown flag for cancellation (will be updated with tasks later)
        with processing_lock:
            if user_id not in user_processing_tasks:
                user_processing_tasks[user_id] = {}
            user_processing_tasks[user_id]["shutdown_flag"] = shutdown_flag
            user_processing_tasks[user_id]["type"] = "video_file"
        
        # Send processing started message
        await bot.send_message(chat_id=chat_id, text=get_text(metadata, "processing_started"))
        
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
                from youtube_live_transcription.whisper_transcriber import WhisperTranscriber
                from youtube_live_transcription.language_model import LMProcessor
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
                
                # Transcribe (silently)
                if transcriber.use_faster_whisper:
                    segments, info = model.transcribe(audio_path, beam_size=5)
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
                    if hasattr(args, 'lm_output_mode') and args.lm_output_mode != "transcriptions-only":
                        try:
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
                                report = lm_processor.process_transcription_window(transcription_data)
                                if report:
                                    # Send LM Report label as separate message
                                    print(f"\n{get_text(metadata, 'lm_report_label')}")
                                    print(report)
                        except Exception as e:
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
                                await send_text_to_telegram(bot, chat_id, buffer)
                            break
                        continue
                    
                    if text is None:
                        if buffer.strip():
                            await send_text_to_telegram(bot, chat_id, buffer)
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
                        await send_text_to_telegram(bot, chat_id, buffer)
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
            
            # Update task info with executor task
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
        except Exception as e:
            logger.exception(f"Error in pipeline execution: {e}")
            await bot.send_message(chat_id=chat_id, text=f"{get_text(metadata, 'error_occurred')} {str(e)}")
        finally:
            # Wait for output task to finish (with timeout)
            try:
                await asyncio.wait_for(output_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for output task")
                output_task.cancel()
                try:
                    await output_task
                except asyncio.CancelledError:
                    pass
        
        # Only send completion message if not cancelled
        if not shutdown_flag.is_set():
            try:
                await bot.send_message(chat_id=chat_id, text=get_text(metadata, "processing_complete"))
            except Exception as e:
                logger.debug(f"Error sending completion message: {e}")
        
        # Clean up video file
        try:
            if os.path.exists(video_path):
                os.remove(video_path)
        except Exception as e:
            logger.warning(f"Error cleaning up video file: {e}")
        
        # Reset context after processing
        reset_context()
        
        # Clear processing state
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
        
    except asyncio.CancelledError:
        logger.info(f"Processing cancelled for user {user_id}")
        try:
            await bot.send_message(chat_id=chat_id, text=get_text(metadata, "cancel_success"))
        except Exception as e:
            logger.debug(f"Error sending cancel message: {e}")
        # Reset context on cancel
        reset_context()
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]
    except Exception as e:
        logger.exception(f"Error processing video file: {e}")
        error_text = get_text(metadata, "error_occurred")
        try:
            await bot.send_message(chat_id=chat_id, text=f"{error_text} {str(e)}")
        except Exception as e2:
            logger.debug(f"Error sending error message: {e2}")
        # Reset context on error
        reset_context()
        with processing_lock:
            if user_id in user_processing_tasks:
                del user_processing_tasks[user_id]


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages (authentication, YouTube links, button clicks, context, tokens)"""
    user_id = update.effective_user.id
    text = update.message.text
    metadata = context.bot_data.get("metadata", load_metadata())
    keyboard = context.bot_data.get("keyboard")
    
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
                    await update.message.reply_text(
                        get_text(metadata, "auth_success"),
                        reply_markup=keyboard
                    )
                else:
                    await update.message.reply_text(get_text(metadata, "invalid_key"))
            except Exception as e:
                logger.error(f"Error checking auth key: {e}")
                await update.message.reply_text(get_text(metadata, "error_occurred") + f" {str(e)}")
        else:
            await update.message.reply_text(get_text(metadata, "not_authenticated"))
        return
    
    # Handle button clicks
    context_btn_text = get_text(metadata, "context_button")
    cancel_btn_text = get_text(metadata, "cancel_button")
    
    if text == context_btn_text:
        await handle_context_button(update, context)
        return
    elif text == cancel_btn_text:
        await cancel_command(update, context)
        return
    
    # Handle state-based inputs
    state = get_user_state(user_id)
    
    if state == STATE_WAITING_CONTEXT:
        # User is entering context
        update_context(text)
        set_user_state(user_id, None)
        await update.message.reply_text(
            get_text(metadata, "context_set"),
            reply_markup=keyboard
        )
        return
    # Check if it's a YouTube URL
    if is_youtube_url(text):
        # Check if already processing
        with processing_lock:
            if user_id in user_processing_tasks:
                await update.message.reply_text(get_text(metadata, "processing_video"))
                return
        
        # Set processing state
        set_user_state(user_id, STATE_PROCESSING)
        
        # Check capacity at start of processing
        capacity, should_warn = check_capacity_at_start()
        if should_warn:
            warning_text = get_text(metadata, "token_warning")
            await update.message.reply_text(warning_text)
        
        # Send initial processing message
        chat_id = update.effective_chat.id
        try:
            await update.message.reply_text(get_text(metadata, "processing_youtube"))
            logger.info(f"Initial processing message sent successfully to chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Failed to send initial processing message to chat_id={chat_id}: {e}", exc_info=True)
        
        # Start processing in background
        task = asyncio.create_task(process_youtube_video(text, user_id, update, context))
        
        # Update task info (shutdown_flag already set in process_youtube_video)
        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task
                user_processing_tasks[user_id]["type"] = "youtube"
            else:
                user_processing_tasks[user_id] = {"task": task, "type": "youtube", "shutdown_flag": threading.Event()}
        
        # Clear processing state when done
        try:
            await task
        finally:
            set_user_state(user_id, None)
        return
    
    # Check if it's a Twitter/X.com URL
    if is_twitter_url(text):
        # Check if already processing
        with processing_lock:
            if user_id in user_processing_tasks:
                await update.message.reply_text(get_text(metadata, "processing_video"))
                return
        
        # Set processing state
        set_user_state(user_id, STATE_PROCESSING)
        
        # Check capacity at start of processing
        capacity, should_warn = check_capacity_at_start()
        if should_warn:
            warning_text = get_text(metadata, "token_warning")
            await update.message.reply_text(warning_text)
        
        # Send initial processing message
        chat_id = update.effective_chat.id
        try:
            await update.message.reply_text(get_text(metadata, "processing_twitter"))
            logger.info(f"Initial processing message sent successfully to chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Failed to send initial processing message to chat_id={chat_id}: {e}", exc_info=True)
        
        # Start processing in background
        task = asyncio.create_task(process_twitter_video(text, user_id, update, context))
        
        # Update task info (shutdown_flag already set in process_twitter_video)
        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task
                user_processing_tasks[user_id]["type"] = "twitter"
            else:
                user_processing_tasks[user_id] = {"task": task, "type": "twitter", "shutdown_flag": threading.Event()}
        
        # Clear processing state when done
        try:
            await task
        finally:
            set_user_state(user_id, None)
        
        return
    
    # Unknown text
    await update.message.reply_text(
        get_text(metadata, "unknown_text"),
        reply_markup=keyboard
    )


async def handle_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle video file uploads"""
    user_id = update.effective_user.id
    metadata = context.bot_data.get("metadata", load_metadata())
    keyboard = context.bot_data.get("keyboard")
    
    # Check authentication
    if not is_user_authenticated(user_id):
        await update.message.reply_text(get_text(metadata, "not_authenticated"))
        return
    
    # Check if already processing
    with processing_lock:
        if user_id in user_processing_tasks:
            await update.message.reply_text(get_text(metadata, "processing_video"))
            return
    
    # Get video file
    video = update.message.video or update.message.document
    if not video:
        await update.message.reply_text(get_text(metadata, "no_video_file"))
        return
    
    # Check token warning
    if check_token_warning():
        warning_text = get_text(metadata, "token_warning")
        await update.message.reply_text(warning_text)
    
    # Set processing state
    set_user_state(user_id, STATE_PROCESSING)
    
    await update.message.reply_text(get_text(metadata, "downloading_video"))
    
    # Download video file
    try:
        video_file = await context.bot.get_file(video.file_id)
        temp_dir = tempfile.gettempdir()
        video_path = os.path.join(temp_dir, f"telegram_video_{user_id}_{video.file_id}.{video.file_name.split('.')[-1] if video.file_name else 'mp4'}")
        
        await video_file.download_to_drive(video_path)
        
        # Process video
        task = asyncio.create_task(process_video_file(video_path, user_id, update, context))
        
        # Update task info
        with processing_lock:
            if user_id in user_processing_tasks:
                user_processing_tasks[user_id]["task"] = task
                user_processing_tasks[user_id]["type"] = "file"
            else:
                user_processing_tasks[user_id] = {"task": task, "type": "file"}
        
        # Clear processing state when done
        try:
            await task
        finally:
            set_user_state(user_id, None)
            
    except Exception as e:
        logger.exception(f"Error handling video: {e}")
        error_text = get_text(metadata, "error_occurred")
        await update.message.reply_text(f"{error_text} {str(e)}", reply_markup=keyboard)
        set_user_state(user_id, None)


async def handle_context_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle context button click"""
    user_id = update.effective_user.id
    metadata = context.bot_data.get("metadata", load_metadata())
    
    set_user_state(user_id, STATE_WAITING_CONTEXT)
    await update.message.reply_text(get_text(metadata, "enter_context"))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors"""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    
    # Try to send error message to user if update is available
    if isinstance(update, Update) and update.effective_message:
        metadata = context.bot_data.get("metadata", load_metadata())
        error_text = get_text(metadata, "error_occurred")
        try:
            await update.effective_message.reply_text(f"{error_text} {str(context.error)}")
        except Exception:
            pass


def setup_handlers(application: Application, metadata: dict, keyboard: ReplyKeyboardMarkup):
    """Setup all handlers for the bot application"""
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    # Handle button clicks (they come as text messages)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text_message
    ))
    
    # Handle video files
    application.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO,
        handle_video_message
    ))
    
    # Add error handler
    application.add_error_handler(error_handler)

