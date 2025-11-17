# Changelog

All notable changes to TRNS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-01-XX

### Added
- Initial release of TRNS
- CLI interface: `trns <url>` command
- Telegram bot with FastAPI webhook support
- Support for YouTube, Twitter/X.com, and local video files
- Automatic subtitle extraction
- Whisper-based speech-to-text transcription
- Automatic translation to Russian
- Language model processing via OpenRouter.ai
- Daily capacity tracking (1000 requests/day)
- Real-time transcription updates via Telegram
- Environment variable support for configuration
- Docker support for deployment
- Comprehensive documentation (English and Russian)
- Architecture documentation
- User guide for Telegram bot (Russian)

### Changed
- Project renamed from V2R to TRNS
- Restructured codebase into proper Python package (`src/trns/`)
- Migrated from polling to webhook-based Telegram bot
- Improved error handling and logging
- Enhanced token management system

### Removed
- Old polling-based Telegram bot (`telegram_bot.py`)
- Old handlers file (`telegram_bot_handlers.py`)
- Token rotation functionality (replaced with daily capacity)

### Fixed
- Audio extraction errors in Telegram bot
- Output delivery issues (now sends on completion, not just cancel)
- Graceful shutdown handling
- Import path issues

## [Unreleased]

### Planned
- Database integration for persistent state
- Redis caching support
- Multi-language metadata support
- Admin web interface
- Analytics and usage statistics

