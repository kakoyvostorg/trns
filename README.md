# TRNS — Video Transcription & Summarization

Transcribe YouTube, Twitter/X, and local video files. Automatic translation to Russian, LLM summaries via OpenRouter. Works as a CLI tool or Telegram bot.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) |
| Video download | [yt-dlp](https://github.com/yt-dlp/yt-dlp) (YouTube, Twitter/X, 1000+ sites) |
| Subtitles | [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) |
| Translation | [deep-translator](https://github.com/nidhaloff/deep-translator) (Google Translate) |
| LLM processing | [OpenRouter.ai](https://openrouter.ai/) via OpenAI client |
| Telegram bot | [Pyrogram](https://pyrogram.org/) (MTProto, up to 2 GB file downloads) |
| Webhook server | [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn |

## Quick Start

### Install

```bash
pip install trns
# Also need FFmpeg:
# macOS: brew install ffmpeg
# Linux: sudo apt install ffmpeg
```

### CLI

```bash
trns https://www.youtube.com/watch?v=VIDEO_ID
trns https://twitter.com/user/status/1234567890
trns /path/to/video.mp4

# With options
trns https://youtu.be/abc --whisper-model medium --debug
```

### Telegram Bot

```bash
export BOT_TOKEN=...            # from @BotFather
export TELEGRAM_API_ID=...      # from my.telegram.org
export TELEGRAM_API_HASH=...    # from my.telegram.org
export AUTH_KEY=secret123       # users authenticate with this once
# For internet-facing production also set:
# export TRNS_ENV=production
# export WEBHOOK_SECRET=...
# export ADMIN_TOKEN=...

python -m trns.bot.server
```

Then set up a webhook pointing to `https://your-domain/webhook`. See [Setup Guide](docs/SETUP.md).

---

## Configuration

TRNS uses a JSON config file (`config.json`). On first run, a default is created automatically. You can also pass everything via CLI flags — **explicit CLI flags always win** over config values, which in turn override parser defaults. If you don't pass a flag, the config value is used; if the config doesn't set it either, the built-in default applies.

### Configuration Reference

Every key in `config.json` maps to a CLI flag. Here's what each one does:

| Key | CLI Flag | Type | Default | Description |
|-----|----------|------|---------|-------------|
| `url` | positional arg | string | `""` | Video URL or local file path. Empty = must provide on command line. |
| `method` | `--method` | `"auto"` \| `"subtitles"` \| `"whisper"` | `"auto"` | **auto**: try YouTube captions first, fall back to Whisper. **subtitles**: captions only (fails if unavailable). **whisper**: always use speech-to-text. |
| `interval` | `--interval` | integer (seconds) | `30` | Chunk duration for live/chunked processing. Each chunk is this many seconds of audio. |
| `language` | `--language` | string (ISO 639-1) | `"en"` | Expected language of the video. Used for subtitle extraction and as a hint for Whisper. |
| `whisper_model` | `--whisper-model` | `"auto"` \| `"tiny"` \| `"base"` \| `"small"` \| `"medium"` \| `"large"` | `"auto"` | Whisper model size. `auto` (default) picks tiny for English, small for other languages. Explicit values override auto-selection. Larger = more accurate but slower and uses more RAM. |
| `use_faster_whisper` | `--use-faster-whisper` / `--no-faster-whisper` | boolean | `true` | Use the faster-whisper library (CTranslate2 backend). Use `--no-faster-whisper` to fall back to openai-whisper. |
| `translation_output` | `--translation-output` | `"russian-only"` \| `"both"` \| `"original-only"` | `"russian-only"` | What to print for transcription output. **russian-only**: only the Russian translation. **both**: original + Russian. **original-only**: no translation. |
| `save_transcript` | `--save-transcript` | string (file path) \| `null` | `null` | If set, appends all output to this file. Relative paths resolve against `TRNS_HOME` / CWD. |
| `overlap` | `--overlap` | integer (seconds) | `2` | Overlap between audio chunks. Prevents words from being cut at chunk boundaries. |
| `process_mode` | `--process-mode` | `"auto"` \| `"chunked"` \| `"full"` | `"auto"` | **auto**: full for regular videos, chunked for live streams. **full**: download entire video, transcribe with progress bar. **chunked**: process in `interval`-second pieces (required for live). |
| `lm_window_seconds` | `--lm-window-seconds` | integer (seconds) | `120` | How much transcription context the LLM sees. It gets the last `ceil(window_seconds / interval)` chunks. |
| `lm_interval` | `--lm-interval` | integer (seconds) | `30` | How often the LLM processes accumulated text. Can differ from `interval`. |
| `lm_output_mode` | `--lm-output-mode` | `"both"` \| `"transcriptions-only"` \| `"lm-only"` | `"both"` | **both**: print transcriptions AND LLM summaries. **transcriptions-only**: skip LLM entirely. **lm-only**: only show LLM output. |
| `lm_api_key_file` | `--lm-api-key-file` | string (file path) | `"api_key.txt"` | File containing OpenRouter API key. |
| `lm_prompt_file` | `--lm-prompt-file` | string (file path) | `"prompt.md"` | Prompt template for Russian-language LLM processing. |
| `lm_model` | `--lm-model` | string | `"google/gemma-3-27b-it:free"` | OpenRouter model identifier. See [openrouter.ai/models](https://openrouter.ai/models) for options. Free models have `:free` suffix. |
| `timecode` | `--timecode` | boolean | `false` | Prefix emitted transcription lines with `[MM:SS]` or `[HH:MM:SS]`. In the Telegram bot, this is the operator default for each user's Timecodes toggle. |
| `timecode_in_summary` | `--timecode-in-summary` | boolean | `false` | Inject sparse timecode markers into the transcript window sent to the LM so summaries can cite moments. |
| `timecode_min_interval_seconds` | `--timecode-min-interval-seconds` | number | `30.0` | Minimum source-video seconds between LM prompt timecode markers. Use `0` to allow every anchor. |
| `debug` | `--debug` | boolean | `false` | **false** (production): logs go to `logs.txt`, stdout shows only transcription/LLM output. **true**: verbose logs go to stderr, useful for troubleshooting. |
| `context` | `--context` | string | `""` | Additional context passed to the LLM (e.g. "This is a Sberbank earnings call"). Helps the model produce better summaries. |
| `allowed_user_ids` | — | array of integers | `[]` | Optional legacy/preauthorized Telegram user IDs. New runtime authentications are stored in per-user files under `TRNS_STATE_DIR`. |

### Example config.json

```json
{
  "url": "",
  "method": "auto",
  "interval": 30,
  "language": "en",
  "whisper_model": "medium",
  "use_faster_whisper": true,
  "translation_output": "russian-only",
  "save_transcript": null,
  "overlap": 2,
  "process_mode": "full",
  "lm_window_seconds": 120,
  "lm_interval": 30,
  "lm_output_mode": "both",
  "lm_api_key_file": "api_key.txt",
  "lm_prompt_file": "prompt.md",
  "lm_model": "google/gemma-3-27b-it:free",
  "timecode": false,
  "timecode_in_summary": false,
  "timecode_min_interval_seconds": 30.0,
  "debug": false,
  "context": "",
  "allowed_user_ids": []
}
```

### Environment Variables

These are primarily for the Telegram bot server, not the CLI:

| Variable | Purpose | Fallback file |
|----------|---------|---------------|
| `BOT_TOKEN` | Telegram bot token | `bot_key.txt` |
| `TELEGRAM_API_ID` | Pyrogram MTProto API ID (from my.telegram.org) | — |
| `TELEGRAM_API_HASH` | Pyrogram MTProto API hash | — |
| `AUTH_KEY` | One-time auth key for new bot users | `key.txt` |
| `OPENROUTER_API_KEY` | OpenRouter API key (alternative to `api_key.txt`) | `api_key.txt` |
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `CONFIG_PATH` | Path to `config.json` | `config.json` |
| `METADATA_PATH` | Path to `metadata.json` | `metadata.json` |
| `TRNS_HOME` | Base directory for resolving all relative paths | CWD |
| `TRNS_STATE_DIR` | Runtime state directory for per-user state and daily capacity | `state` under `TRNS_HOME` |
| `TRNS_ENV` | Set to `production` to require webhook/admin secrets at startup | `development` |
| `WEBHOOK_SECRET` | Telegram webhook secret token; required when `TRNS_ENV=production` | — |
| `ADMIN_TOKEN` | Bearer token for `/set_webhook` and `/webhook_info`; required when `TRNS_ENV=production` | — |

### File Layout

When you run TRNS, it expects these files relative to `TRNS_HOME` (defaults to your current working directory):

```
your-project/
├── config.json          # Main configuration (auto-created on first run)
├── metadata.json        # Localization strings + daily capacity counter
├── api_key.txt          # OpenRouter API key(s), one per line
├── prompt.md            # LLM prompt template (Russian output)
├── prompt_original.md   # LLM prompt template (original language output)
├── bot_key.txt          # Telegram bot token (alternative to env var)
├── key.txt              # Auth key (alternative to env var)
├── logs.txt             # Production logs (auto-created)
└── state/               # Runtime state (ignored by git)
    ├── global.json      # Daily LM capacity counter
    └── users/
        └── <telegram_id>.json  # Per-user auth/settings/context
```

---

## How It Works

### Pipeline Flow

```
Video URL or file
    │
    ├─ 1. Try YouTube auto-captions (if method=auto and it's YouTube)
    │     └─ Success? Skip Whisper, go to step 3
    │
    ├─ 2. Download audio → Whisper speech-to-text
    │     ├─ Language auto-detection
    │     ├─ Chunk overlap to prevent word loss
    │     └─ Progress bar (full mode) or streaming (chunked mode)
    │
    ├─ 3. Translate to Russian (if source ≠ Russian)
    │     └─ Google Translate via deep-translator
    │
    └─ 4. LLM summarization (if lm_output_mode ≠ transcriptions-only)
          ├─ Sends last N seconds of transcription (lm_window_seconds)
          ├─ Bilingual mode: separate prompts for original + Russian
          └─ Output: structured summary per prompt template
```

### Processing Modes

| Mode | When | How |
|------|------|-----|
| **full** | Regular videos | Downloads entire video first, transcribes with progress bar. Best quality. |
| **chunked** | Live streams | Processes audio in `interval`-second chunks. Real-time output. |
| **auto** | Default | Picks `full` for regular videos, `chunked` for live streams. |

### Whisper Models

| Model | Size | Speed | Quality | RAM |
|-------|------|-------|---------|-----|
| `tiny` | 39M | ~32x realtime | Basic | ~1 GB |
| `base` | 74M | ~16x realtime | OK | ~1 GB |
| `small` | 244M | ~6x realtime | Good | ~2 GB |
| `medium` | 769M | ~2x realtime | Very good | ~5 GB |
| `large` | 1550M | ~1x realtime | Best | ~10 GB |

### API Key Setup

Put your OpenRouter API key in `api_key.txt`. The system tracks daily usage capacity in `metadata.json` and resets it at UTC midnight.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   User Interface                     │
├────────────────────┬────────────────────────────────┤
│   CLI (trns cmd)   │  Telegram Bot (Pyrogram+FastAPI)│
└─────────┬──────────┴──────────────┬─────────────────┘
          └────────────┬────────────┘
                       │
         ┌─────────────▼──────────────┐
         │   TranscriptionPipeline    │
         │   (orchestration + threads)│
         └─────────────┬──────────────┘
                       │
     ┌─────────────────┼─────────────────┐
     │                 │                 │
┌────▼─────┐   ┌──────▼──────┐   ┌──────▼──────┐
│  yt-dlp  │   │   faster-   │   │ OpenRouter  │
│  audio   │   │   whisper   │   │    LLM      │
│ download │   │   STT       │   │  summaries  │
└──────────┘   └─────────────┘   └─────────────┘
```

### Threading (Telegram Bot)

The bot uses a queue-based architecture for thread-safe output:

1. Webhook arrives → FastAPI handler → spawns background thread
2. Background thread runs `TranscriptionPipeline` with an `output_callback`
3. `output_callback` puts text into a `queue.Queue`
4. Async loop drains the queue and sends messages to Telegram

No global state mutation — each pipeline instance is independent.

---

## Development

```bash
git clone https://github.com/kakoyvostorg/trns.git
cd trns
pip install -e ".[dev]"
pytest                    # 117 tests
ruff format .             # code formatting
```

## Docker

```bash
docker build -f docker/Dockerfile -t trns .
docker-compose -f docker/docker-compose.yml up
```

## Further Reading

- [Setup Guide](docs/SETUP.md) — installation, FFmpeg, webhook config
- [Deployment Guide](docs/DEPLOYMENT.md) — production deployment (Yandex Cloud, VMs, Docker)
- [Architecture](docs/ARCHITECTURE.md) — detailed system internals
- [Руководство пользователя](docs/USER_GUIDE_RU.md) — Telegram bot user guide (Russian)

## License

MIT — see [LICENSE](LICENSE).
