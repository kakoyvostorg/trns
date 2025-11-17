#!/usr/bin/env python3
"""
FastAPI Telegram Bot with Webhook Support

This bot provides Telegram interface for the YouTube transcription functionality
using FastAPI and webhooks instead of polling.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application

from trns.bot.utils import load_metadata, get_text
from trns.bot.routes import setup_handlers

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global bot application
bot_application = None


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


def create_keyboard(metadata: dict) -> ReplyKeyboardMarkup:
    """Create persistent keyboard with buttons"""
    context_btn = KeyboardButton(get_text(metadata, "context_button"))
    cancel_btn = KeyboardButton(get_text(metadata, "cancel_button"))
    
    keyboard = [[context_btn], [cancel_btn]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for FastAPI app"""
    global bot_application
    
    # Startup: Initialize bot application
    try:
        bot_token = get_bot_token()
        logger.info("Bot token loaded successfully")
        
        metadata = load_metadata()
        logger.info("Metadata loaded successfully")
        
        keyboard = create_keyboard(metadata)
        
        # Build application
        bot_application = Application.builder().token(bot_token).build()
        
        # Setup handlers
        setup_handlers(bot_application, metadata, keyboard)
        
        # Store metadata and keyboard in bot data
        bot_application.bot_data["metadata"] = metadata
        bot_application.bot_data["keyboard"] = keyboard
        
        # Initialize application
        await bot_application.initialize()
        
        logger.info("Bot application initialized successfully")
        logger.info("⚠️  IMPORTANT: Bot will not receive updates until webhook is configured!")
        logger.info("   Use POST /set_webhook with your webhook URL to enable the bot.")
        logger.info("   For testing, use ngrok: ngrok http 8000")
        
        yield
        
    except Exception as e:
        logger.exception(f"Error during startup: {e}")
        raise
    finally:
        # Shutdown: Cleanup all processing tasks
        try:
            from telegram_bot_routes import user_processing_tasks, processing_lock, cancel_user_processing
            logger.info("Cancelling all ongoing processing tasks...")
            with processing_lock:
                user_ids = list(user_processing_tasks.keys())
            for user_id in user_ids:
                try:
                    await cancel_user_processing(user_id)
                except Exception as e:
                    logger.error(f"Error cancelling task for user {user_id}: {e}")
        except Exception as e:
            logger.error(f"Error during task cleanup: {e}")
        
        # Shutdown bot application
        if bot_application:
            try:
                logger.info("Shutting down bot application...")
                # Only stop if running
                if hasattr(bot_application, 'running') and bot_application.running:
                    await bot_application.stop()
                await bot_application.shutdown()
                logger.info("Bot application shut down successfully")
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")


# Create FastAPI app
app = FastAPI(
    title="YouTube Transcription Telegram Bot",
    description="Telegram bot for YouTube live transcription using webhooks",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "bot_initialized": bot_application is not None}


@app.post("/webhook")
async def webhook(request: Request):
    """Webhook endpoint for receiving Telegram updates"""
    global bot_application
    
    if bot_application is None:
        logger.error("Bot application not initialized")
        return JSONResponse(
            status_code=503,
            content={"error": "Bot application not initialized"}
        )
    
    try:
        # Parse update from request
        update_data = await request.json()
        update = Update.de_json(update_data, bot_application.bot)
        
        if update is None:
            logger.warning("Received invalid update")
            return Response(status_code=200)
        
        # Process update
        await bot_application.process_update(update)
        
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
    global bot_application
    
    if bot_application is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Bot application not initialized"}
        )
    
    try:
        await bot_application.bot.set_webhook(
            url=request.webhook_url,
            secret_token=request.secret_token
        )
        logger.info(f"Webhook set to: {request.webhook_url}")
        return {"status": "success", "webhook_url": request.webhook_url}
    except Exception as e:
        logger.exception(f"Error setting webhook: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.get("/webhook_info")
async def get_webhook_info():
    """Get current webhook information"""
    global bot_application
    
    if bot_application is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Bot application not initialized"}
        )
    
    try:
        webhook_info = await bot_application.bot.get_webhook_info()
        return {
            "url": webhook_info.url,
            "has_custom_certificate": webhook_info.has_custom_certificate,
            "pending_update_count": webhook_info.pending_update_count,
            "last_error_date": webhook_info.last_error_date,
            "last_error_message": webhook_info.last_error_message,
            "max_connections": webhook_info.max_connections,
            "allowed_updates": webhook_info.allowed_updates
        }
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
    logger.info("Press Ctrl+C to stop")
    
    # Let uvicorn handle signals naturally - it will trigger lifespan shutdown
    uvicorn.run(
        app, 
        host=host, 
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()

