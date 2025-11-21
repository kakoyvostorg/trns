# TRNS Setup Guide

This guide will help you set up TRNS for both CLI usage and Telegram bot deployment.

## Prerequisites

- Python 3.8 or higher
- FFmpeg installed on your system
- (For Telegram bot) A Telegram bot token from [@BotFather](https://t.me/botfather)
- (For LM processing) An OpenRouter.ai API key

## Installation

### From PyPI (Recommended)

```bash
pip install trns
```

### From Source

```bash
git clone https://github.com/yourusername/trns.git
cd trns
pip install -e .
```

## FFmpeg Installation

TRNS requires FFmpeg for audio processing:

**macOS:**
```bash
brew install ffmpeg
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update
sudo apt-get install ffmpeg
```

**Windows:**
Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH.

## CLI Setup

The CLI requires minimal setup. Just ensure you have:

1. FFmpeg installed
2. (Optional) OpenRouter.ai API key in `api_key.txt` or `OPENROUTER_API_KEY` environment variable

### Basic Usage

```bash
trns https://www.youtube.com/watch?v=VIDEO_ID
```

## Telegram Bot Setup

### 1. Create a Telegram Bot

1. Open Telegram and search for [@BotFather](https://t.me/botfather)
2. Send `/newbot` and follow instructions
3. Save the bot token you receive

### 2. Configure Authentication

Choose one of the following methods:

#### Method A: Environment Variables (Recommended for Production)

Create a `.env` file or set environment variables:

```bash
export BOT_TOKEN=your_bot_token_here
export TELEGRAM_API_ID=your_api_id_here
export TELEGRAM_API_HASH=your_api_hash_here
export AUTH_KEY=your_auth_key_here
export OPENROUTER_API_KEY=your_openrouter_api_key
```

**Getting Telegram API Credentials:**
1. Go to https://my.telegram.org
2. Log in with your phone number
3. Click on "API development tools"
4. Create a new application (if you haven't already)
5. Copy your `api_id` and `api_hash`

**User Authentication:**
Users authenticate by sending the `AUTH_KEY` to the bot. After successful authentication, their user ID is automatically stored in `config.json` and they remain authenticated.

#### Method B: File-based Configuration

Create the following files in your project root:

- `bot_key.txt`: Your Telegram bot token
- `key.txt`: Authentication key (users will need this to access the bot)
- `api_key.txt`: OpenRouter.ai API key (one per line if multiple)

### 3. Configure Metadata and Config Files

Copy example files and customize:

```bash
cp config/metadata.example.json metadata.json
cp config/config.example.json config.json
```

Edit `metadata.json` to customize bot messages and `config.json` for application settings.

### 4. Run the Bot

#### Local Development (with ngrok)

1. Start the bot:
   ```bash
   python -m trns.bot.server
   ```

2. In another terminal, start ngrok:
   ```bash
   ngrok http 8000
   ```

3. Set the webhook:
   ```bash
   curl -X POST "http://localhost:8000/set_webhook" \
     -H "Content-Type: application/json" \
     -d '{"webhook_url": "https://your-ngrok-url.ngrok-free.dev/webhook"}'
   ```

4. Verify webhook:
   ```bash
   curl http://localhost:8000/webhook_info
   ```

#### Production Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for cloud deployment instructions.

### 5. Test the Bot

1. Open Telegram and find your bot
2. Send `/start` - you should receive an authentication prompt
3. Send your authentication key
4. Send a YouTube URL or video file to test transcription

## Troubleshooting

### Bot Not Responding

- Check that webhook is set correctly: `curl http://localhost:8000/webhook_info`
- Verify bot token is correct
- Check server logs for errors
- Ensure ngrok is running (for local development)

### Authentication Issues

- Verify `AUTH_KEY` or `key.txt` matches what you're sending
- Check `ALLOWED_USER_IDS` or `allowed_ids.txt` includes your user ID
- Your Telegram user ID can be found by messaging [@userinfobot](https://t.me/userinfobot)

### Transcription Errors

- Ensure FFmpeg is installed and in PATH: `ffmpeg -version`
- Check that video URL is accessible
- Verify OpenRouter.ai API key is valid

### Import Errors

If you get import errors after installation:

```bash
# Reinstall in development mode
pip install -e .

# Or ensure you're in the correct directory
cd /path/to/trns
python -m trns.bot.server
```

## Next Steps

- Read [DEPLOYMENT.md](DEPLOYMENT.md) for production deployment
- Check [ARCHITECTURE.md](ARCHITECTURE.md) for system architecture
- See [USER_GUIDE_RU.md](USER_GUIDE_RU.md) for Telegram bot usage guide (Russian)

