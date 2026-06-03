"""Pyrogram update dispatcher."""
import logging

from pyrogram import Client
from pyrogram.types import Update

from ._commands import cancel_command, start_command, stats_command
from ._handlers import handle_text_message, handle_video_message

logger = logging.getLogger(__name__)


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
            raw_command = message.text.split()[0].lower()
            command = raw_command.split('@')[0]

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
