#!/usr/bin/env python3
"""
FastAPI Telegram Bot with Pyrogram and Webhook Support

This bot provides Telegram interface for the YouTube transcription functionality
using FastAPI webhooks with Pyrogram MTProto client.
"""

import io
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pyrogram import Client
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton, Update
import pyrogram.raw

from trns.bot.utils import load_metadata, get_text, get_user_setting

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


def get_bot_token(bot_key_path: str = "bot_key.txt") -> str:
    """Load bot token from environment variable or file"""
    # Try environment variable first
    token = os.getenv("BOT_TOKEN")
    if token:
        return token.strip()
    
    # Fallback to file
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
    
    # Add context and cancel buttons
    keyboard.append([context_btn])
    keyboard.append([cancel_btn])
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for FastAPI app"""
    global bot_client, bot_metadata, bot_keyboard
    
    # Startup: Initialize bot client
    try:
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
            from trns.bot.routes import user_processing_tasks, processing_lock, cancel_user_processing
            
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
    """Health check endpoint"""
    return {"status": "healthy", "bot_initialized": bot_client is not None}


@app.post("/webhook")
async def webhook(request: Request):
    """Webhook endpoint for receiving Telegram updates"""
    global bot_client
    
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
        
        # Create a simple message wrapper for Bot API data
        # This mimics Pyrogram's Message structure for our handlers
        msg_data = update_data["message"]
        
        class BotAPIMessage:
            """Lightweight wrapper for Bot API messages to work with our handlers"""
            def __init__(self, client, data):
                self._client = client
                self._data = data
                self.message_id = data.get("message_id")
                self.date = data.get("date")
                self.chat = type('obj', (object,), {'id': data.get("chat", {}).get("id")})()
                self.text = data.get("text")
                self.caption = data.get("caption")
                
                # User info
                from_data = data.get("from", {})
                self.from_user = type('obj', (object,), {
                    'id': from_data.get("id"),
                    'is_bot': from_data.get("is_bot", False),
                    'first_name': from_data.get("first_name", ""),
                    'username': from_data.get("username")
                })()
                
                # Video/document info
                self.video = None
                self.document = None
                
                if "video" in data:
                    vid_data = data["video"]
                    self.video = type('obj', (object,), {
                        'file_id': vid_data.get("file_id"),
                        'file_unique_id': vid_data.get("file_unique_id"),
                        'file_size': vid_data.get("file_size"),
                        'file_name': vid_data.get("file_name"),
                        'mime_type': vid_data.get("mime_type")
                    })()
                
                if "document" in data:
                    doc_data = data["document"]
                    self.document = type('obj', (object,), {
                        'file_id': doc_data.get("file_id"),
                        'file_unique_id': doc_data.get("file_unique_id"),
                        'file_size': doc_data.get("file_size"),
                        'file_name': doc_data.get("file_name"),
                        'mime_type': doc_data.get("mime_type")
                    })()
            
            async def reply_text(self, text, reply_markup=None):
                """Reply to this message"""
                return await self._client.send_message(
                    chat_id=self.chat.id,
                    text=text,
                    reply_markup=reply_markup
                )
            
            async def download(self, file_name=None):
                """Download file (video/document) - supports up to 2GB via MTProto"""
                file_id = None
                default_ext = '.mp4'
                
                if self.video:
                    file_id = self.video.file_id
                    if self.video.file_name:
                        import os
                        default_ext = os.path.splitext(self.video.file_name)[1] or '.mp4'
                elif self.document:
                    file_id = self.document.file_id
                    if self.document.file_name:
                        import os
                        default_ext = os.path.splitext(self.document.file_name)[1] or '.bin'
                
                if not file_id:
                    return None
                
                # Generate file name if not provided
                if not file_name:
                    import tempfile
                    import os
                    file_name = os.path.join(tempfile.gettempdir(), f"download_{file_id}{default_ext}")
                
                # Use Pyrogram's download_media which uses MTProto (supports up to 2GB)
                # Pyrogram can download using file_id directly
                downloaded_path = await self._client.download_media(
                    message=file_id,
                    file_name=file_name
                )
                
                return downloaded_path
        
        # Create message wrapper
        message = BotAPIMessage(bot_client, msg_data)
        
        # Create simple update object
        class SimpleUpdate:
            def __init__(self, msg):
                self.message = msg
        
        update = SimpleUpdate(message)
        
        # Route to handlers
        from trns.bot.routes import route_update
        await route_update(bot_client, update)
        
        return Response(status_code=200)
        
    except Exception as e:
        logger.exception(f"Error processing webhook update: {e}")
        return Response(status_code=200)  # Return 200 to prevent Telegram retries


class WebhookRequest(BaseModel):
    webhook_url: str
    secret_token: str = None


@app.post("/set_webhook")
async def set_webhook(request: WebhookRequest):
    """Set webhook URL for Telegram bot"""
    global bot_client
    
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
        
        payload = {"url": request.webhook_url}
        if request.secret_token:
            payload["secret_token"] = request.secret_token
        
        async with aiohttp.ClientSession() as session:
            async with session.post(telegram_api_url, json=payload) as response:
                result = await response.json()
                
                if result.get("ok"):
                    logger.info(f"Webhook set to: {request.webhook_url}")
                    return {"status": "success", "webhook_url": request.webhook_url}
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
async def get_webhook_info():
    """Get current webhook information"""
    global bot_client
    
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
