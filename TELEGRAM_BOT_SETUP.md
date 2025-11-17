# Telegram Bot Setup Guide (FastAPI Webhooks)

This guide explains how to set up and run the new FastAPI-based Telegram bot with webhook support.

## Overview

The bot has been completely rewritten to use:
- **FastAPI** for the webhook server
- **Webhooks** instead of polling (more efficient)
- **Queue-based output** instead of complex stdout redirection (fixes the audio extraction error)

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure you have the required files:
   - `bot_key.txt` - Your Telegram bot token
   - `key.txt` - Authentication key for users
   - `metadata.json` - Bot text translations
   - `config.json` - Transcription configuration
   - `api_key.txt` - OpenRouter API keys (for LM processing)

## Running the Bot

### Option 1: Direct Run

```bash
python telegram_bot_fastapi.py
```

The server will start on `http://0.0.0.0:8000` by default.

### Option 2: Using Uvicorn

```bash
uvicorn telegram_bot_fastapi:app --host 0.0.0.0 --port 8000
```

### Environment Variables

- `PORT` - Server port (default: 8000)
- `HOST` - Server host (default: 0.0.0.0)

## Setting Up Webhooks

### 1. Expose Your Server

You need to expose your local server to the internet. Options:

**Option A: Using ngrok (for testing)**
```bash
ngrok http 8000
```

**Option B: Using a public server**
Deploy to a server with a public IP/domain.

### 2. Set the Webhook

Once your server is accessible, set the webhook:

**Using the API endpoint:**
```bash
curl -X POST "http://localhost:8000/set_webhook" \
  -H "Content-Type: application/json" \
  -d '{"webhook_url": "https://your-domain.com/webhook"}'
```

**Or using Telegram Bot API directly:**
```bash
curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-domain.com/webhook"}'
```

### 3. Verify Webhook

Check webhook status:
```bash
curl http://localhost:8000/webhook_info
```

## API Endpoints

- `POST /webhook` - Main webhook endpoint (Telegram sends updates here)
- `GET /health` - Health check endpoint
- `POST /set_webhook` - Set webhook URL
- `GET /webhook_info` - Get current webhook information

## Key Differences from Old Implementation

1. **No stdout redirection issues**: Uses queue-based message passing
2. **Webhooks instead of polling**: More efficient and scalable
3. **FastAPI server**: Modern async framework
4. **Better error handling**: Proper async/await patterns

## Troubleshooting

### Webhook not receiving updates

1. Check webhook status: `GET /webhook_info`
2. Verify your server is accessible from the internet
3. Check server logs for errors
4. Ensure HTTPS is used (Telegram requires HTTPS for webhooks)

### Audio extraction errors

The new implementation uses a queue-based approach that should fix the "[ERROR] Failed to extract audio chunk at 0.0s" error. If you still see this:

1. Check that ffmpeg is installed and in PATH
2. Verify yt-dlp is up to date: `pip install --upgrade yt-dlp`
3. Check server logs for detailed error messages

### Bot not responding

1. Check health endpoint: `GET /health`
2. Verify bot token is correct in `bot_key.txt`
3. Check server logs for authentication errors

## Migration from Old Bot

The old files (`telegram_bot.py`, `telegram_bot_handlers.py`) are no longer used. You can archive them but keep `telegram_bot_utils.py` as it's still needed.

## Development

For local development with ngrok:

1. Start the bot: `python telegram_bot_fastapi.py`
2. In another terminal: `ngrok http 8000`
3. Set webhook to ngrok URL: `curl -X POST "http://localhost:8000/set_webhook" -d '{"webhook_url": "https://your-ngrok-url.ngrok.io/webhook"}'`

