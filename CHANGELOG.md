# Changelog

All notable changes to TRNS will be documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Thread-safety**: replaced `builtins.print` monkey-patching with per-pipeline `output_callback` — concurrent Telegram users no longer cross-contaminate output
- **Overlap removal**: translated text was sliced by original-language character count, corrupting output. Now uses independent word-matching for each language
- **Off-by-one in capacity tracking**: last daily request was consumed but reported as failed (`capacity > 0` → `capacity >= 0`)
- **Token staleness**: after key rotation, LM client kept using old API key. Now detects change and reinitializes
- **`_load_prompt()` typo**: called non-existent method after language detection (`_load_prompt` → `_load_prompts`)
- **Missing user settings for YouTube**: `show_original_translation` preference was only loaded for Twitter, not YouTube
- **Temp file leak on shutdown**: audio extraction temp files left on disk when shutdown triggered mid-process
- **Bot file path resolution**: relative paths like `api_key.txt` now resolve against `TRNS_HOME`/CWD consistently

### Changed
- Extracted `_run_pipeline_with_output()` and `_process_url_video()` — eliminated ~400 lines of duplication in `routes.py`
- Moved inline `type('obj', ...)` hacks to proper module-level classes in `server.py`
- Added thread-safe locking for `user_states` dict
- Centralized token refresh logic in `_get_token_and_decrement()`
- Rewrote README with complete configuration reference table
- Updated documentation

## [0.1.1] - 2025-11-17

### Fixed
- Fixed package build configuration (package-dir, license format)
- Fixed PyPI publishing workflow to support manual publishing
- Removed deprecated license classifier

## [0.1.0] - 2025-01-15

### Added
- Initial release of TRNS
- CLI interface: `trns <url>` command
- Telegram bot with FastAPI webhook support
- Support for YouTube, Twitter/X.com, and local video files
- Automatic subtitle extraction + Whisper speech-to-text
- Automatic translation to Russian
- Language model processing via OpenRouter.ai
- Daily capacity tracking (1000 requests/day)
- Docker support
- Documentation (English and Russian)

### Changed
- Project renamed from V2R to TRNS
- Restructured into proper Python package (`src/trns/`)
- Migrated from polling to webhook-based Telegram bot
