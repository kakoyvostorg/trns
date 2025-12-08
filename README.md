# TRNS - Transcription and Language Model Processing

TRNS is a powerful tool for transcribing YouTube videos, Twitter/X.com videos, and local video files with automatic translation and language model processing. It provides both a command-line interface and a Telegram bot for easy access.

## Tech Stack

| Component | Technology |
|-----------|------------|
| **Telegram Bot** | [Pyrogram](https://pyrogram.org/) (MTProto) — enables large file downloads up to 2GB |
| **Web Server** | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) with webhook support |
| **Speech-to-Text** | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — high-performance Whisper implementation |
| **Video Download** | [yt-dlp](https://github.com/yt-dlp/yt-dlp) — supports YouTube, Twitter/X, and 1000+ sites |
| **Subtitles** | [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) — auto-generated captions extraction |
| **Translation** | [deep-translator](https://github.com/nidhaloff/deep-translator) — multi-provider translation |
| **LLM Processing** | [OpenRouter.ai](https://openrouter.ai/) via OpenAI client — intelligent summaries |
| **Audio Processing** | FFmpeg |

## Features

- 🎥 **Multi-source support**: YouTube videos, Twitter/X.com videos, and local video files (up to 2GB via Telegram)
- 🗣️ **Speech-to-text**: High-performance transcription with faster-whisper
- 📝 **Smart subtitle fallback**: Uses auto-generated captions when available, falls back to Whisper
- 🌍 **Automatic translation**: Translates transcriptions to Russian
- 🤖 **Language model processing**: Processes transcriptions through OpenRouter.ai for intelligent summaries
- 📱 **Telegram bot**: Interactive bot with real-time transcription updates via Pyrogram MTProto
- 🖥️ **CLI tool**: Simple command-line interface: `trns <url>`
- ⚡ **Async processing**: Background task processing with graceful shutdown
- 🔐 **Authentication**: User whitelist with AUTH_KEY-based onboarding

## Quick Start

### Installation

```bash
pip install trns
```

### CLI Usage

```bash
# Transcribe a YouTube video
trns https://www.youtube.com/watch?v=VIDEO_ID

# Transcribe a Twitter/X.com video
trns https://twitter.com/user/status/1234567890

# Transcribe a local video file
trns /path/to/video.mp4
```

### Telegram Bot Setup

1. Create a Telegram bot via [@BotFather](https://t.me/botfather)
2. Get API credentials from [my.telegram.org](https://my.telegram.org) (required for Pyrogram MTProto)
3. Set environment variables or create config files:
   ```bash
   export BOT_TOKEN=your_bot_token
   export TELEGRAM_API_ID=your_api_id       # From my.telegram.org
   export TELEGRAM_API_HASH=your_api_hash   # From my.telegram.org
   export AUTH_KEY=your_auth_key
   export OPENROUTER_API_KEY=your_api_key
   ```
   
   **Note:** `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are required for Pyrogram's MTProto client, which enables downloading files up to 2GB (vs 20MB with Bot API).
4. Run the bot:
   ```bash
   python -m trns.bot.server
   ```
5. Configure webhook (see [SETUP.md](docs/SETUP.md) for details)

## Configuration

TRNS supports both environment variables and file-based configuration:

### Environment Variables

- `BOT_TOKEN`: Telegram bot token (from @BotFather)
- `TELEGRAM_API_ID`: Telegram API ID (from https://my.telegram.org)
- `TELEGRAM_API_HASH`: Telegram API Hash (from https://my.telegram.org)
- `AUTH_KEY`: Authentication key for bot access (users authenticate once, then stored in config.json)
- `OPENROUTER_API_KEY`: OpenRouter.ai API key
- `HOST`: Server host (default: 0.0.0.0)
- `PORT`: Server port (default: 8000)
- `CONFIG_PATH`: Path to config.json (default: config.json)
- `METADATA_PATH`: Path to metadata.json (default: metadata.json)

**Note:** Authenticated user IDs are stored in `config.json` after successful authentication with the AUTH_KEY.

### File-based Configuration

Create the following files in the project root:

- `bot_key.txt`: Telegram bot token
- `key.txt`: Authentication key
- `api_key.txt`: OpenRouter.ai API key (one per line)
- `config.json`: Application configuration (copy from `config/config.example.json`)

**Important:** Copy `config/config.example.json` to `config.json` and customize it. The `config.json` file is not tracked in git as it contains sensitive user IDs after authentication.
- `metadata.json`: Localization and metadata

See `config/` directory for example files.

## Requirements

- Python 3.8+
- FFmpeg (for audio processing)
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt-get install ffmpeg`
  - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      User Interface                          │
├───────────────────────┬─────────────────────────────────────┤
│   CLI Interface       │     Telegram Bot Interface          │
│   (trns command)      │     (Pyrogram + FastAPI Webhooks)   │
└───────────┬───────────┴──────────────┬──────────────────────┘
            │                          │
            └──────────┬───────────────┘
                       │
         ┌─────────────▼─────────────┐
         │   Transcription Pipeline  │
         └─────────────┬─────────────┘
                       │
    ┌──────────────────┼──────────────────┐
    │                  │                  │
┌───▼────────┐  ┌──────▼──────┐  ┌───────▼───────┐
│   yt-dlp   │  │   faster-   │  │  OpenRouter   │
│   Audio    │  │   whisper   │  │     LLM       │
│ Extraction │  │ Transcriber │  │   Processor   │
└────────────┘  └─────────────┘  └───────────────┘
```

## Documentation

- [Setup Guide](docs/SETUP.md) - Detailed setup instructions
- [Deployment Guide](docs/DEPLOYMENT.md) - Cloud deployment instructions
- [Architecture](docs/ARCHITECTURE.md) - System architecture documentation
- [Architecture (Russian)](docs/ARCHITECTURE_RU.md) - Архитектура системы
- [User Guide (Russian)](docs/USER_GUIDE_RU.md) - Руководство пользователя Telegram бота

## Development

```bash
# Clone the repository
git clone https://github.com/kakoyvostorg/trns.git
cd trns

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest

# Format code
ruff format .

# Type checking
mypy src/
```

## Docker

```bash
# Build image
docker build -f docker/Dockerfile -t trns .

# Run with docker-compose
docker-compose -f docker/docker-compose.yml up
```

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please read our contributing guidelines and submit pull requests.

## Support

For issues and questions, please open an issue on GitHub.
