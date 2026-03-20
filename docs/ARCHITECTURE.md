# TRNS Architecture

## Overview

TRNS transcribes video (YouTube, Twitter/X, local files) into text, translates it to Russian, and optionally summarizes it with an LLM. Two interfaces: CLI and Telegram bot. Both share the same `TranscriptionPipeline`.

## System Diagram

```
┌──────────────────────────────────────────────────────┐
│                    User Interface                      │
├───────────────────┬──────────────────────────────────┤
│  CLI (trns cmd)   │  Telegram Bot (Pyrogram + FastAPI)│
└────────┬──────────┴───────────────┬──────────────────┘
         └───────────┬──────────────┘
                     │
       ┌─────────────▼─────────────┐
       │   TranscriptionPipeline   │
       │   (orchestration layer)   │
       └─────────────┬─────────────┘
                     │
    ┌────────────────┼────────────────┐
    │                │                │
┌───▼──────┐  ┌─────▼──────┐  ┌─────▼──────┐
│  yt-dlp  │  │  faster-   │  │ OpenRouter │
│  audio   │  │  whisper   │  │   LLM      │
│ download │  │   STT      │  │ summaries  │
└──────────┘  └────────────┘  └────────────┘
```

## Components

### 1. CLI (`trns.transcription.main`)

Entry point: `trns <url>` (or `python -m trns.transcription.main`)

1. Parse CLI args + load `config.json`
2. Resolve file paths against `TRNS_HOME` (defaults to CWD)
3. Create `TranscriptionPipeline` with `output_callback=None` (uses `print()`)
4. Run pipeline, output goes to stdout
5. Logs go to `logs.txt` (production) or stderr (`--debug`)

### 2. Telegram Bot (`trns.bot`)

**Files:**
- `server.py` — FastAPI app, webhook endpoint, Pyrogram client lifecycle
- `routes.py` — message/command handlers, pipeline orchestration
- `utils.py` — auth, token management, config, capacity tracking
- `output_handler.py` — Telegram message sending with rate limiting

**How webhook processing works:**

```
Telegram → POST /webhook → FastAPI
    │
    ├─ Parse Bot API JSON into BotAPIMessage wrapper
    ├─ Route to handler (command, URL, file upload)
    │
    ├─ Handler spawns background thread:
    │   ┌─────────────────────────────────────┐
    │   │  output_queue = Queue()              │
    │   │  pipeline = TranscriptionPipeline(   │
    │   │      output_callback=queue.put)      │
    │   │  pipeline.run()                      │
    │   └─────────────────────────────────────┘
    │
    └─ Async loop drains queue → sends to Telegram
```

**Key design decisions:**

- **No `builtins.print` patching.** Each pipeline gets its own `output_callback`. Two concurrent users never interfere.
- **Thread-safe user states.** `user_states` dict protected by `_user_states_lock`.
- **BotAPIMessage wrapper.** Webhook receives raw Bot API JSON; lightweight wrapper classes (`BotAPIMessage`, `_ChatInfo`, etc.) provide a Pyrogram-compatible interface without parsing overhead.

### 3. TranscriptionPipeline (`trns.transcription.pipeline`)

The core orchestrator. ~1300 lines covering:

**Initialization:**
- Creates Whisper transcriber, subtitle extractor, LM processor
- Sets up inter-thread queues (`transcription_queue`, `lm_queue`)
- Stores `output_callback` for all text output

**Full-video mode flow:**
1. Download entire video audio via yt-dlp
2. Run faster-whisper with progress bar
3. Auto-detect language
4. Translate to Russian (if not Russian)
5. Send to LLM for summarization
6. Output results via `self._output()`

**Chunked/live mode flow:**
1. Extract audio in `interval`-second chunks with `overlap`-second overlap
2. Transcribe each chunk
3. Remove overlapping words (independent word-matching for original + translated text)
4. Accumulate chunks for LLM window
5. Every `lm_interval` seconds, send accumulated text to LLM
6. Output transcription + LLM results in real-time

**Subtitle fallback (method=auto):**
- Try `youtube-transcript-api` first (instant, no Whisper needed)
- If captions unavailable, fall back to Whisper

### 4. Whisper Transcriber (`trns.transcription.whisper_transcriber`)

- Uses `faster-whisper` (CTranslate2 backend)
- Auto-detects language from first audio chunk
- Supports all Whisper model sizes (tiny → large)
- Cleans up temp files on shutdown (`_cleanup_temp_files()`)
- Handles both full-video and chunk-based extraction

### 5. Language Model Processor (`trns.transcription.language_model`)

- Calls OpenRouter.ai API via OpenAI-compatible client
- **Token management**: reads keys from `api_key.txt`, rotates on exhaustion
- **Daily capacity**: tracked in `metadata.json`, resets at UTC midnight
- **Client refresh**: detects when token has changed and reinitializes the OpenAI client
- **Bilingual mode**: two prompts — one for original language, one for Russian
- **Prompt files**: `prompt.md` (Russian), `prompt_original.md` (original language)

### 6. Utils (`trns.bot.utils`)

- `get_current_token()` / `decrement_daily_capacity()` — token rotation + capacity
- `load_metadata()` / `save_metadata()` — thread-safe metadata I/O
- `get_text(metadata, key)` — localized string lookup from `metadata.json`
- User settings (per-user preferences stored in `config.json`)

## Configuration Priority

```
CLI flag (explicit) > config.json value > default
```

Exception: `--url` on command line always wins over `config.json` URL.

All relative file paths (`api_key.txt`, `prompt.md`, etc.) resolve against `TRNS_HOME` environment variable, which defaults to the current working directory.

## Threading Model

### CLI
Single-threaded. Pipeline runs synchronously, output goes to stdout.

### Telegram Bot

```
Main thread:     FastAPI/Uvicorn server
Per-user thread: TranscriptionPipeline.run()
                 └─ output_callback → Queue → async drain → Telegram API
```

Thread safety mechanisms:
- `threading.Lock` for `user_states`, `processing_lock`, metadata file I/O
- `threading.Event` for shutdown signaling
- `queue.Queue` for cross-thread output (pipeline thread → async event loop)

## Error Handling

| Layer | Strategy |
|-------|----------|
| Audio extraction | Retry with consecutive failure limit (default: 3) |
| Whisper | Fallback from subtitles to Whisper (method=auto) |
| OpenRouter API | Log error, skip LLM output, continue transcription |
| Telegram send | Log error, continue processing |
| Capacity exhausted | Warn user, stop LLM processing, keep transcribing |

## File Reference

```
src/trns/
├── transcription/
│   ├── main.py              # CLI entry point, arg parsing, config loading
│   ├── pipeline.py          # TranscriptionPipeline orchestrator
│   ├── whisper_transcriber.py  # Audio extraction + Whisper STT
│   ├── language_model.py    # OpenRouter LLM processing
│   └── subtitle_extractor.py   # YouTube captions extraction
├── bot/
│   ├── server.py            # FastAPI app, webhook, Pyrogram client
│   ├── routes.py            # Message handlers, pipeline launching
│   ├── utils.py             # Auth, tokens, config, capacity
│   └── output_handler.py    # Telegram message sending
└── cli/
    └── main.py              # CLI wrapper (calls transcription.main)
```
