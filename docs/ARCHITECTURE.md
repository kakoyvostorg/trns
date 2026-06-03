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
- `routes/` — package: message/command handlers and pipeline orchestration (see below)
- `utils.py` — auth, token management, config, capacity tracking
- `output_handler.py` — Telegram message sending with rate limiting

**`trns.bot.routes` package** (public API re-exported from `routes/__init__.py`; imports like `from trns.bot import routes` are unchanged):

| Module | Responsibility |
|--------|----------------|
| `_state.py` | Shared locks, `user_processing_tasks`, conversation state, `TRNS_HOME` path resolution for args |
| `_commands.py` | `/start`, `/cancel`, `/stats`, cooperative `cancel_user_processing` |
| `_processing.py` | URL detection, `_run_pipeline_with_output`, `_process_url_video`, `process_video_file` |
| `_handlers.py` | Text/video messages, inline keyboard callbacks |
| `_dispatcher.py` | `route_update` — dispatches Pyrogram updates to handlers |

**Design note (uploads vs URLs):** YouTube, Twitter/X, and **uploaded Telegram videos** share one orchestration path: `_process_url_video` / `process_video_file` call `_run_pipeline_with_output`, which runs `TranscriptionPipeline` with `video_id` set to the extracted ID, the page URL, or a **local file path** (uploads). The pipeline treats existing files as local input, skips YouTube subtitles, and runs Whisper/LM like URL jobs. Compared to the older upload-only codepath, audio extraction and decoding now follow the pipeline (yt-dlp/FFmpeg), full-video Whisper uses its default beam size (e.g. 5), and text formatting follows `_output_transcription` / `_output_lm_report`.

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
- **Video metadata prefix**: title + description (truncated to `--video-metadata-chars`) are wrapped in a `<video_metadata note="Untrusted...">` block and injected between the prompt and the transcript on every call
- **Timecode-in-summary mode**: when `--timecode-in-summary` is on, `_serialize_window_with_timecodes` interleaves sparse `[MM:SS]` markers (≥`timecode_min_interval_seconds` apart) into the transcript window, and a short bilingual instruction is appended to both prompts at load time

### 5a. Anchor sidecars (timecode mode)

Timecode mode uses a **sidecar** data shape: the `text` / `translated` fields stay plain strings, and a parallel `anchors` / `anchors_translated` list carries `{offset, time}` markers where `offset` is a char index into the text and `time` is the video-absolute second (for live streams, seconds since the start of capture).

- **Builder**: `pipeline._build_anchors(segments, chunk_start=0.0)` accepts whisper `Segment` objects or `{start, end, text}` dicts and returns `(text, anchors)`. It reproduces the existing `" ".join(...).strip()` text contract so consumers that ignore anchors see no difference
- **Chunk-start convention**: 0 for full-video; `self.current_time_position` (pre-increment) for chunked non-live; `self.live_capture_seconds` counter for live streams
- **Translated-side anchors**: `WhisperTranscriber.translate_segments_to_russian` batches translations with a `\n|||\n` separator (and falls back to per-segment translation on split mismatch), returning segments that preserve the original timings with Russian text, from which a second `anchors_translated` list is built
- **CLI render**: `_output_transcription` prepends one timecode per emit (head anchor of the current result); the subtitle path wires the prefix inline using the same helper
- **LM render**: sparse markers are injected only inside the LM window — not in the raw text buffer — keeping the existing sentence-buffer logic unchanged

### 6. Utils (`trns.bot.utils`)

- `get_current_token()` / `decrement_daily_capacity()` — token selection + daily capacity in runtime `state/global.json`
- `load_metadata()` — localization/default metadata from `metadata.json`
- `get_text(metadata, key)` — localized string lookup from `metadata.json`
- User state (auth/settings/context stored in `state/users/<telegram_id>.json`; `config.json` `allowed_user_ids` is legacy/preauthorization fallback)

## Configuration Priority

```
config.json value > CLI default
```

Exception: `--url` on command line always wins over `config.json` URL.

Note: `config.json` values are applied on top of CLI defaults. To override a config value from the CLI, pass the flag explicitly (e.g., `--whisper-model large`).

All relative file paths (`api_key.txt`, `prompt.md`, etc.) resolve against `TRNS_HOME` environment variable, which defaults to the current working directory. Mutable runtime state resolves under `TRNS_STATE_DIR`, defaulting to `state/` under `TRNS_HOME`.

For the **CLI**, if you do not pass `--config`, the config file is `CONFIG_PATH` if set, otherwise `config.json` (under `TRNS_HOME`), consistent with `trns.transcription.main.load_config(None)`. Passing `--config /path/to/file.json` selects that file explicitly and does not use `CONFIG_PATH`.

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
│   ├── routes/              # Bot handlers package (see Components §2)
│   │   ├── __init__.py      # Re-exports route_update, cancel_user_processing, etc.
│   │   ├── _state.py
│   │   ├── _commands.py
│   │   ├── _processing.py
│   │   ├── _handlers.py
│   │   └── _dispatcher.py
│   ├── utils.py             # Auth, tokens, config, capacity
│   └── output_handler.py    # Telegram message sending
└── cli/
    └── main.py              # CLI wrapper (calls transcription.main)
```
