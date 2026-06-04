#!/usr/bin/env python3
"""
FastAPI Telegram Bot with Pyrogram and Webhook Support

This bot provides Telegram interface for the YouTube transcription functionality
using FastAPI webhooks with Pyrogram MTProto client.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pyrogram import Client
from pyrogram.types import KeyboardButton, ReplyKeyboardMarkup

from trns.bot.utils import (
    _resolve_path,
    get_text,
    get_timecode_default,
    get_user_setting,
    load_metadata,
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global bot client and state
bot_client = None
bot_metadata = None
bot_keyboard = None

# Webhook secret for validating incoming Telegram requests.
# Set via WEBHOOK_SECRET env var; when configured, every POST /webhook
# must carry a matching X-Telegram-Bot-Api-Secret-Token header.
_webhook_secret: Optional[str] = os.getenv("WEBHOOK_SECRET")

# Admin token for management endpoints (/set_webhook, /webhook_info).
# Set via ADMIN_TOKEN env var; when configured, those endpoints require
# an Authorization: Bearer <token> header.
_admin_token: Optional[str] = os.getenv("ADMIN_TOKEN")

_environment = (
    os.getenv("TRNS_ENV")
    or os.getenv("APP_ENV")
    or os.getenv("ENVIRONMENT")
    or "development"
).lower()


def _is_production() -> bool:
    """Return True when production hardening should be required."""
    return _environment in {"prod", "production"}


def _validate_production_security():
    """Fail fast when an internet-facing deployment forgot required secrets."""
    if not _is_production():
        return
    missing = []
    if not _webhook_secret:
        missing.append("WEBHOOK_SECRET")
    if not _admin_token:
        missing.append("ADMIN_TOKEN")
    if missing:
        raise RuntimeError(
            "Production mode requires: "
            + ", ".join(missing)
            + ". Set TRNS_ENV=development for local testing."
        )


def get_bot_token(bot_key_path: str = "bot_key.txt") -> str:
    """Load bot token from environment variable or file"""
    # Try environment variable first
    token = os.getenv("BOT_TOKEN")
    if token:
        return token.strip()

    # Fallback to file
    bot_key_path = _resolve_path(bot_key_path)
    try:
        with open(bot_key_path, 'r', encoding='utf-8') as f:
            token = f.read().strip()
            if not token:
                raise ValueError("Bot token is empty")
            return token
    except FileNotFoundError:
        logger.error(f"Bot key file not found: {bot_key_path} and BOT_TOKEN environment variable not set")
        raise
    except Exception as e:
        logger.error(f"Error loading bot token: {e}")
        raise


def get_api_id() -> int:
    """Load Telegram API ID from environment variable"""
    api_id = os.getenv("TELEGRAM_API_ID")
    if not api_id:
        raise ValueError("TELEGRAM_API_ID environment variable not set. Get it from https://my.telegram.org")
    try:
        return int(api_id)
    except ValueError:
        raise ValueError(f"TELEGRAM_API_ID must be a number, got: {api_id}")


def get_api_hash() -> str:
    """Load Telegram API hash from environment variable"""
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH environment variable not set. Get it from https://my.telegram.org")
    return api_hash.strip()


def create_keyboard(metadata: dict, user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """
    Create persistent keyboard with buttons.
    If user_id is provided, includes dynamic toggle buttons based on user settings.

    Args:
        metadata: Metadata dict with translations
        user_id: Optional user ID for personalized buttons

    Returns:
        ReplyKeyboardMarkup with buttons
    """
    context_btn = KeyboardButton(get_text(metadata, "context_button"))
    cancel_btn = KeyboardButton(get_text(metadata, "cancel_button"))

    keyboard = []

    # Add toggle buttons if user_id is provided
    if user_id is not None:
        # Get user settings (defaults to True)
        show_original = get_user_setting(user_id, "show_original_translation", default=True)
        show_transcription = get_user_setting(user_id, "show_transcription", default=True)
        timecode_enabled = get_user_setting(
            user_id,
            "timecode_enabled",
            default=get_timecode_default(),
        )

        # Toggle button for original translation
        if show_original:
            original_btn_text = get_text(metadata, "hide_original_translation_button")
        else:
            original_btn_text = get_text(metadata, "show_original_translation_button")
        original_btn = KeyboardButton(original_btn_text)

        # Toggle button for transcription
        if show_transcription:
            transcription_btn_text = get_text(metadata, "hide_transcription_button")
        else:
            transcription_btn_text = get_text(metadata, "show_transcription_button")
        transcription_btn = KeyboardButton(transcription_btn_text)

        # Add toggle buttons in first row (side by side)
        keyboard.append([original_btn, transcription_btn])

        if timecode_enabled:
            timecode_btn_text = get_text(metadata, "hide_timecodes_button")
        else:
            timecode_btn_text = get_text(metadata, "show_timecodes_button")
        keyboard.append([KeyboardButton(timecode_btn_text)])

    # Add context and cancel buttons
    keyboard.append([context_btn])
    keyboard.append([cancel_btn])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


class _ChatInfo:
    """Minimal chat info wrapper."""
    __slots__ = ('id',)
    def __init__(self, chat_id): self.id = chat_id

class _UserInfo:
    """Minimal user info wrapper."""
    __slots__ = ('id', 'is_bot', 'first_name', 'username')
    def __init__(self, data):
        self.id = data.get("id")
        self.is_bot = data.get("is_bot", False)
        self.first_name = data.get("first_name", "")
        self.username = data.get("username")

class _FileInfo:
    """Minimal file (video/document) info wrapper."""
    __slots__ = ('file_id', 'file_unique_id', 'file_size', 'file_name', 'mime_type')
    def __init__(self, data):
        self.file_id = data.get("file_id")
        self.file_unique_id = data.get("file_unique_id")
        self.file_size = data.get("file_size")
        self.file_name = data.get("file_name")
        self.mime_type = data.get("mime_type")

class BotAPIMessage:
    """Lightweight wrapper for Bot API messages to work with our handlers."""
    def __init__(self, client, data):
        self._client = client
        self._data = data
        self.message_id = data.get("message_id")
        self.date = data.get("date")
        self.chat = _ChatInfo(data.get("chat", {}).get("id"))
        self.text = data.get("text")
        self.caption = data.get("caption")
        self.from_user = _UserInfo(data.get("from", {}))
        self.video = _FileInfo(data["video"]) if "video" in data else None
        self.document = _FileInfo(data["document"]) if "document" in data else None

    async def reply_text(self, text, reply_markup=None):
        return await self._client.send_message(
            chat_id=self.chat.id, text=text, reply_markup=reply_markup
        )

    async def download(self, file_name=None):
        file_id = None
        default_ext = '.mp4'
        if self.video:
            file_id = self.video.file_id
            if self.video.file_name:
                default_ext = os.path.splitext(self.video.file_name)[1] or '.mp4'
        elif self.document:
            file_id = self.document.file_id
            if self.document.file_name:
                default_ext = os.path.splitext(self.document.file_name)[1] or '.bin'
        if not file_id:
            return None
        if not file_name:
            import tempfile
            file_name = os.path.join(tempfile.gettempdir(), f"download_{file_id}{default_ext}")
        return await self._client.download_media(message=file_id, file_name=file_name)

class SimpleUpdate:
    """Minimal update wrapper."""
    __slots__ = ('message',)
    def __init__(self, msg): self.message = msg


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for FastAPI app"""
    global bot_client, bot_metadata, bot_keyboard

    # Startup: Initialize bot client
    try:
        _validate_production_security()

        bot_token = get_bot_token()
        api_id = get_api_id()
        api_hash = get_api_hash()
        logger.info("Bot credentials loaded successfully")

        bot_metadata = load_metadata()
        logger.info("Metadata loaded successfully")

        # Create default keyboard without user_id (will be recreated per user)
        bot_keyboard = create_keyboard(bot_metadata, user_id=None)

        # Build Pyrogram client with in-memory session to avoid file corruption
        bot_client = Client(
            "trns_bot",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            workdir="/tmp",  # Store session files in /tmp for serverless
            in_memory=True  # Use in-memory session to prevent corruption from concurrent access
        )

        # Start client
        await bot_client.start()

        logger.info("Bot client initialized successfully")
        logger.info("⚠️  IMPORTANT: Bot will not receive updates until webhook is configured!")
        logger.info("   Use POST /set_webhook with your webhook URL to enable the bot.")
        logger.info("   For testing, use ngrok: ngrok http 8000")

        yield

    except Exception as e:
        logger.exception(f"Error during startup: {e}")
        raise
    finally:
        # Shutdown: Cleanup all processing tasks
        logger.info("=" * 60)
        logger.info("SHUTTING DOWN - Please wait...")
        logger.info("=" * 60)

        try:
            from trns.bot.routes import (
                cancel_user_processing,
                processing_lock,
                user_processing_tasks,
            )

            with processing_lock:
                num_tasks = len(user_processing_tasks)
                user_ids = list(user_processing_tasks.keys())

            if num_tasks > 0:
                logger.info(f"Cancelling {num_tasks} ongoing processing task(s)...")
                logger.info("This may take up to 10 seconds for active transcriptions...")

                for user_id in user_ids:
                    try:
                        await cancel_user_processing(user_id)
                    except Exception as e:
                        logger.error(f"Error cancelling task for user {user_id}: {e}")

                logger.info("All processing tasks cancelled")
            else:
                logger.info("No active processing tasks to cancel")

        except Exception as e:
            logger.error(f"Error during task cleanup: {e}")

        # Shutdown bot client
        if bot_client:
            try:
                logger.info("Shutting down Pyrogram client...")
                await bot_client.stop()
                logger.info("✓ Bot client shut down successfully")
            except Exception as e:
                logger.error(f"Error during client shutdown: {e}")

        logger.info("=" * 60)
        logger.info("SHUTDOWN COMPLETE")
        logger.info("=" * 60)


# Create FastAPI app
app = FastAPI(
    title="YouTube Transcription Telegram Bot",
    description="Telegram bot for YouTube live transcription using Pyrogram and webhooks",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Health check endpoint.

    Returns 200 + healthy only when the bot is fully initialized.
    Returns 503 + degraded when the process is up but the bot is not ready.
    """
    if bot_client is None:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "bot_initialized": False}
        )
    return {"status": "healthy", "bot_initialized": True}


@app.post("/webhook")
async def webhook(request: Request):
    """Webhook endpoint for receiving Telegram updates.

    When WEBHOOK_SECRET is set, the request must include a matching
    X-Telegram-Bot-Api-Secret-Token header (standard Telegram mechanism).
    """
    global bot_client

    # Validate webhook secret when configured
    if _webhook_secret:
        incoming_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming_token != _webhook_secret:
            logger.warning("Rejected webhook request: invalid or missing secret token")
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    if bot_client is None:
        logger.error("Bot client not initialized")
        return JSONResponse(
            status_code=503,
            content={"error": "Bot client not initialized"}
        )

    try:
        # Parse update from request (Bot API format)
        update_data = await request.json()
        logger.debug(f"Received update: {update_data.get('update_id', 'unknown')}")

        # Check if there's a message in the update
        if "message" not in update_data:
            logger.debug("Received update without message, ignoring")
            return Response(status_code=200)

        msg_data = update_data["message"]
        message = BotAPIMessage(bot_client, msg_data)
        update = SimpleUpdate(message)

        # Route to handlers
        from trns.bot.routes import route_update
        await route_update(bot_client, update)

        return Response(status_code=200)

    except Exception as e:
        logger.exception(f"Error processing webhook update: {e}")
        return Response(status_code=200)  # Return 200 to prevent Telegram retries


def _check_admin_auth(request: Request) -> Optional[JSONResponse]:
    """Validate admin Bearer token when ADMIN_TOKEN is configured.

    Returns a 403 JSONResponse if the token is wrong/missing, or None if OK.
    """
    if not _admin_token:
        return None
    auth_header = request.headers.get("Authorization", "")
    if auth_header == f"Bearer {_admin_token}":
        return None
    logger.warning("Rejected admin request: invalid or missing Authorization header")
    return JSONResponse(status_code=403, content={"error": "Forbidden"})


class WebhookRequest(BaseModel):
    webhook_url: str
    secret_token: Optional[str] = None


@app.post("/set_webhook")
async def set_webhook(body: WebhookRequest, request: Request):
    """Set webhook URL for Telegram bot (requires ADMIN_TOKEN when configured)."""
    global bot_client

    denied = _check_admin_auth(request)
    if denied:
        return denied

    if bot_client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Bot client not initialized"}
        )

    try:
        # Use Telegram Bot API directly via HTTP (Pyrogram doesn't have webhook methods for bots)
        import aiohttp
        bot_token = get_bot_token()
        telegram_api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"

        payload = {"url": body.webhook_url}
        secret_token = body.secret_token or _webhook_secret
        if secret_token:
            payload["secret_token"] = secret_token

        async with aiohttp.ClientSession() as session:
            async with session.post(telegram_api_url, json=payload) as response:
                result = await response.json()

                if result.get("ok"):
                    logger.info(f"Webhook set to: {body.webhook_url}")
                    return {"status": "success", "webhook_url": body.webhook_url}
                else:
                    error_msg = result.get("description", "Unknown error")
                    logger.error(f"Failed to set webhook: {error_msg}")
                    return JSONResponse(
                        status_code=400,
                        content={"error": error_msg}
                    )
    except Exception as e:
        logger.exception(f"Error setting webhook: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.get("/webhook_info")
async def get_webhook_info(request: Request):
    """Get current webhook information (requires ADMIN_TOKEN when configured)."""
    global bot_client

    denied = _check_admin_auth(request)
    if denied:
        return denied

    if bot_client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Bot client not initialized"}
        )

    try:
        # Use Telegram Bot API directly via HTTP (Pyrogram doesn't have webhook methods for bots)
        import aiohttp
        bot_token = get_bot_token()
        telegram_api_url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"

        async with aiohttp.ClientSession() as session:
            async with session.get(telegram_api_url) as response:
                result = await response.json()

                if result.get("ok"):
                    info = result.get("result", {})
                    return {
                        "url": info.get("url", ""),
                        "has_custom_certificate": info.get("has_custom_certificate", False),
                        "pending_update_count": info.get("pending_update_count", 0),
                        "last_error_date": info.get("last_error_date"),
                        "last_error_message": info.get("last_error_message"),
                        "max_connections": info.get("max_connections"),
                        "allowed_updates": info.get("allowed_updates")
                    }
                else:
                    error_msg = result.get("description", "Unknown error")
                    return JSONResponse(
                        status_code=400,
                        content={"error": error_msg}
                    )
    except Exception as e:
        logger.exception(f"Error getting webhook info: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


def main():
    """Main entry point for running the bot server"""
    try:
        import uvicorn
    except ImportError:
        raise ImportError("uvicorn is required. Install with: pip install uvicorn[standard]")

    # Get port from environment or use default
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(f"Starting FastAPI server on {host}:{port}")
    logger.info("=" * 60)
    logger.info("⚠️  SHUTDOWN INSTRUCTIONS:")
    logger.info("  1. Press Ctrl+C ONCE to initiate shutdown")
    logger.info("  2. Wait up to 10 seconds for active transcriptions to stop")
    logger.info("  3. DO NOT press Ctrl+C multiple times!")
    logger.info("=" * 60)

    # Let uvicorn handle signals naturally - it will trigger lifespan shutdown
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
