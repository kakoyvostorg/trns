"""
Telegram Output Handler

Handles sending transcription output directly to Telegram without stdout redirection.
Uses callbacks to send messages directly via bot API.
"""

import asyncio
import logging
from typing import Optional, Callable
from io import StringIO

logger = logging.getLogger(__name__)


class TelegramOutputHandler:
    """
    Handler for sending transcription output to Telegram.
    Replaces stdout redirection with direct callback-based messaging.
    """
    
    def __init__(self, send_message_callback: Callable[[str], None], chat_id: int):
        """
        Initialize output handler.
        
        Args:
            send_message_callback: Async function that takes text and sends it to Telegram
            chat_id: Telegram chat ID to send messages to
        """
        self.send_message_callback = send_message_callback
        self.chat_id = chat_id
        self.buffer = StringIO()
        self.lock = asyncio.Lock()
        self.last_send_time = 0.0
        self.send_interval = 0.5  # Send every 0.5 seconds
        
    async def write(self, text: str):
        """
        Write text to buffer and send to Telegram if needed.
        This method is called by the stdout redirection.
        """
        if not text:
            return
        
        async with self.lock:
            self.buffer.write(text)
            
            # Check if this looks like transcription output (contains timestamp brackets)
            is_transcription = '[' in text and ']' in text
            
            # Send immediately for transcriptions, otherwise batch
            import time
            current_time = time.time()
            should_send = (
                (current_time - self.last_send_time >= self.send_interval) or 
                is_transcription
            )
            
            if should_send and self.buffer.tell() > 0:
                text_to_send = self.buffer.getvalue()
                self.buffer = StringIO()  # Reset buffer
                
                if text_to_send.strip():
                    try:
                        await self.send_message_callback(text_to_send)
                        self.last_send_time = current_time
                    except Exception as e:
                        logger.error(f"Error sending message to Telegram: {e}", exc_info=True)
    
    async def flush(self):
        """Flush buffer - send any remaining content"""
        async with self.lock:
            if self.buffer.tell() > 0:
                text_to_send = self.buffer.getvalue()
                self.buffer = StringIO()
                
                if text_to_send.strip():
                    try:
                        await self.send_message_callback(text_to_send)
                    except Exception as e:
                        logger.error(f"Error flushing message to Telegram: {e}", exc_info=True)
    
    def close(self):
        """Close the handler"""
        pass


async def send_text_to_telegram(bot, chat_id: int, text: str) -> None:
    """
    Send text to Telegram, splitting into chunks if needed.
    
    Args:
        bot: Telegram bot instance
        chat_id: Chat ID to send message to
        text: Text to send
    """
    if not text or not text.strip():
        return
    
    # Split into chunks if too long (Telegram limit is 4096 chars)
    max_length = 4000
    chunks = []
    
    while len(text) > max_length:
        chunk = text[:max_length]
        # Try to break at newline
        last_newline = chunk.rfind('\n')
        if last_newline > max_length * 0.7:  # If newline is reasonably close to end
            chunk = text[:last_newline + 1]
            text = text[last_newline + 1:]
        else:
            text = text[max_length:]
        chunks.append(chunk)
    
    if text.strip():
        chunks.append(text)
    
    if not chunks:
        return
    
    # Send all chunks
    for i, chunk in enumerate(chunks):
        if not chunk or not chunk.strip():
            continue
        
        try:
            await bot.send_message(chat_id=chat_id, text=chunk)
            logger.debug(f"Sent chunk {i+1}/{len(chunks)} to chat_id={chat_id}")
        except Exception as e:
            logger.error(f"Error sending message chunk {i+1}/{len(chunks)} to Telegram: {e}", exc_info=True)
            # Continue with other chunks even if one fails
            raise

