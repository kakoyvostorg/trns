# TRNS - Transcription and Language Model Processing

TRNS is a powerful tool for transcribing YouTube videos, Twitter/X.com videos, and local video files with automatic translation and language model processing. It provides both a command-line interface and a Telegram bot for easy access.

## Features

- 🎥 **Multi-source support**: YouTube videos, Twitter/X.com videos, and local video files
- 🗣️ **Speech-to-text**: Uses Whisper for high-quality transcription
- 🌍 **Automatic translation**: Translates transcriptions to Russian
- 🤖 **Language model processing**: Processes transcriptions through OpenRouter.ai for intelligent summaries
- 📱 **Telegram bot**: Interactive bot interface with real-time transcription updates
- 🖥️ **CLI tool**: Simple command-line interface: `trns <url>`

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
2. Set environment variables or create config files:
   ```bash
   export BOT_TOKEN=your_bot_token
   export TELEGRAM_API_ID=your_api_id
   export TELEGRAM_API_HASH=your_api_hash
   export AUTH_KEY=your_auth_key
   export OPENROUTER_API_KEY=your_api_key
   ```
   
   **Note:** Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from https://my.telegram.org. Users will authenticate with `AUTH_KEY` and be automatically added to `config.json`.
3. Run the bot:
   ```bash
   python -m trns.bot.server
   ```
4. Configure webhook (see [SETUP.md](docs/SETUP.md) for details)

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

## Documentation

- [Setup Guide](docs/SETUP.md) - Detailed setup instructions
- [Deployment Guide](docs/DEPLOYMENT.md) - Cloud deployment instructions
- [Architecture](docs/ARCHITECTURE.md) - System architecture documentation
- [Architecture (Russian)](docs/ARCHITECTURE_RU.md) - Архитектура системы
- [User Guide (Russian)](docs/USER_GUIDE_RU.md) - Руководство пользователя Telegram бота

## Development

```bash
# Clone the repository
git clone https://github.com/yourusername/trns.git
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

