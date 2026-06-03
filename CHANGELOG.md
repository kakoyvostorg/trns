# Changelog

All notable changes to TRNS will be documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Video metadata in LM prompt**: title + description of the source video are now captured via `yt-dlp` and prepended to every LM call, wrapped in a `<video_metadata note="Untrusted...">` block so the model treats it as context rather than instructions. Description is truncated to `--video-metadata-chars` (default 2000) chars; set to 0 to disable
- **Timecode mode (`--timecode`)**: prefixes each emitted transcription line with `[MM:SS]` (or `[HH:MM:SS]` on videos ≥1h). Works in the full-video, chunked, live, and subtitle code paths. For live streams timecodes are relative to the start of capture
- **Timecodes in LM summary (`--timecode-in-summary`)**: interleaves sparse `[MM:SS]` markers into the transcript window sent to the LM, and appends a short bilingual instruction to the prompt asking the model to cite them when referencing moments. Marker spacing is tunable via `--timecode-min-interval-seconds` (default 30)
- **Segment-aligned translation**: `WhisperTranscriber.translate_segments_to_russian` batches Google Translate calls with a `\n|||\n` separator and per-segment fallback on split mismatch. Returns translated segments that preserve each original segment's timing, enabling anchors on the Russian side
- **Anchor sidecars on transcription dicts**: each dict now optionally carries `anchors` / `anchors_translated` — lists of `{offset, time}` markers into the respective text. Text shape is unchanged; consumers that don't care about timing ignore them
- **Production security mode**: `TRNS_ENV=production` now requires `WEBHOOK_SECRET` and `ADMIN_TOKEN` at startup; local/default development mode remains permissive for test deploys
- **Runtime state directory**: bot auth/settings/context now live in `state/users/<telegram_id>.json`; daily LM capacity lives in `state/global.json`; `TRNS_STATE_DIR` can override the location

### Changed
- **`WhisperTranscriber.transcribe_audio` return shape**: now a 4-tuple `(text, language, prob, segments)`, where `segments` is a list of `{start, end, text}` dicts. All existing callers have been updated
- **Python support**: supported/tested Python versions are now 3.11, 3.12, and 3.13
- **CI**: GitHub Actions now runs unit tests on Python 3.11/3.12/3.13 and enforces `ruff check .`
- **Anchor smoke check**: `scripts/check_anchors.py` became `scripts/dev_check_anchors.py`, a documented manual dev utility that avoids LM calls by default

## [0.2.1] - 2026-04-02

### Fixed
- **Thread-safety**: replaced `builtins.print` monkey-patching with per-pipeline `output_callback` — concurrent Telegram users no longer cross-contaminate output
- **Overlap removal**: translated text was sliced by original-language character count, corrupting output. Now uses independent word-matching for each language
- **Off-by-one in capacity tracking**: last daily request was consumed but reported as failed (`capacity > 0` → `capacity >= 0`)
- **Token staleness**: after key rotation, LM client kept using old API key. Now detects change and reinitializes
- **`_load_prompt()` typo**: called non-existent method after language detection (`_load_prompt` → `_load_prompts`)
- **Missing user settings for YouTube**: `show_original_translation` preference was only loaded for Twitter, not YouTube
- **Temp file leak on shutdown**: audio extraction temp files left on disk when shutdown triggered mid-process
- **Bot file path resolution**: relative paths like `api_key.txt` now resolve against `TRNS_HOME`/CWD consistently
- **CLI ignored `CONFIG_PATH`**: `--config` used to default to `config.json`, so the variable was never applied for normal `trns` runs. Omitting `--config` now follows `CONFIG_PATH` (then `config.json`), matching `load_config(None)`; an explicit `--config path` still overrides the env var

### Changed
- **Telegram file uploads**: `process_video_file` now uses `_run_pipeline_with_output` / `TranscriptionPipeline` like YouTube/Twitter (removed the duplicated FFmpeg → Whisper/LM → queue path). Audio extraction and decoding follow the pipeline; minor differences vs the old bespoke path include pipeline beam-size defaults and output formatting
- **`trns.bot.routes` package split**: replaced monolithic `routes.py` with `routes/` (`_state`, `_commands`, `_processing`, `_handlers`, `_dispatcher`); `from trns.bot import routes` and re-exports (`route_update`, `cancel_user_processing`, etc.) unchanged
- **Config path alignment**: `trns.transcription.main.load_config` / `create_default_config` use `CONFIG_PATH` or `config.json` when no path argument is given (same rules as `trns.bot.utils.load_config`), avoiding divergent config files when the env var is set; the CLI now omits a default `--config` so it uses the same logic
- **Exception handling**: replaced bare `except:` with typed handlers and debug logging across transcription and bot code paths
- **`.gitignore`**: ignore `output/` and `tmp/`
- Extracted `_run_pipeline_with_output()` and `_process_url_video()` — eliminated ~400 lines of duplication in `routes.py` (historical; code now lives under `routes/_processing.py`)
- Moved inline `type('obj', ...)` hacks to proper module-level classes in `server.py`
- Added thread-safe locking for `user_states` dict
- Centralized token refresh logic in `_get_token_and_decrement()`
- Rewrote README with complete configuration reference table
- Updated documentation

## [0.2.0] - 2026-03-26

### Fixed

- **TRNS_HOME**: file helpers resolve paths against the `TRNS_HOME` environment variable
- **Per-user context**: replaced shared global context with per-user storage in `user_settings.json` and file locking for concurrent safety
- **Whisper model**: default `"auto"` (tiny for English, small for others); explicit `--whisper-model` values honored end-to-end
- **Twitter/X**: pass the real page URL to yt-dlp instead of a synthetic `twitter_` ID that was treated as a YouTube video ID
- **Local files**: pipeline initializes Whisper regardless of `method`, preventing a hang when `method=subtitles` on a local file
- **YouTube Shorts**: `/shorts/` URL pattern added to `is_youtube_url()` and `extract_video_id()`
- **Pipeline early shutdown**: initialize `should_use_whisper` before the main loop to avoid `UnboundLocalError` on early exit
- **LM retry**: daily capacity decremented once per logical request, not once per HTTP retry
- **Telegram `file_size`**: guard arithmetic when `file_size` is `None`
- **Duplicate message**: removed an extra early “processing started” send

### Security

- Webhook validation via `WEBHOOK_SECRET` and header `X-Telegram-Bot-Api-Secret-Token`
- Admin routes via `ADMIN_TOKEN` (Bearer token on `/set_webhook`, `/webhook_info`)
- Command parsing strips `@botName` suffix for group compatibility
- `/stats` uses `DAILY_CAPACITY` / `WARNING_THRESHOLD` constants
- Whisper import fallback shows clear install instructions on `ImportError`
- Health endpoint returns HTTP 503 when the bot is not initialized

### Changed

- **Version**: 0.1.1 → 0.2.0
- **CLI**: `--whisper-model` default changed from `tiny` to `auto`
- **CLI**: `--no-faster-whisper` flag added (mutually exclusive with `--use-faster-whisper`)
- **Config priority**: explicit CLI flags override `config.json`, which overrides parser defaults
- **Docs & packaging**: README, ARCHITECTURE, DEPLOYMENT, and SETUP aligned with runtime behavior; fixed placeholder clone URLs, email typo, and deployment env examples
- **Optional dependency**: `openai-whisper` via `pip install .[whisper]`
- **requirements.txt**: synced with pyproject (e.g. python-dotenv, pydantic)
- **Docker Compose**: set `TRNS_HOME=/app/config` for correct path resolution
- **CI**: GitHub Actions workflow runs pytest on push and pull requests
- **Tests**: fixed timezone mismatch in `TestDailyCapacity` (local date vs UTC); default pytest run excludes integration tests (see `pyproject.toml`); tqdm progress uses `leave=False` and stderr to reduce terminal noise

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
