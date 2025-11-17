# TRNS Architecture Documentation

## Overview

TRNS is a transcription and language model processing system that supports multiple input sources (YouTube, Twitter/X.com, local files) and provides both CLI and Telegram bot interfaces.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        User Interface                        │
├──────────────────────┬──────────────────────────────────────┤
│   CLI Interface      │      Telegram Bot Interface          │
│   (trns command)     │      (FastAPI Webhook Server)        │
└──────────┬───────────┴──────────────┬───────────────────────┘
           │                          │
           └──────────┬───────────────┘
                      │
        ┌─────────────▼─────────────┐
        │   Transcription Pipeline  │
        │   (Orchestration Layer)   │
        └─────────────┬─────────────┘
                      │
        ┌─────────────┼─────────────┐
        │             │             │
┌───────▼──────┐ ┌───▼──────┐ ┌───▼──────────┐
│   Audio      │ │ Whisper  │ │  Language   │
│  Extraction  │ │Transcriber│ │   Model     │
│  (yt-dlp)    │ │           │ │  Processor  │
└──────────────┘ └───────────┘ └─────────────┘
```

## Component Overview

### 1. CLI Interface (`trns.cli.main`)

**Purpose:** Command-line entry point for transcription

**Entry Point:** `trns <url>`

**Flow:**
1. Parses command-line arguments
2. Initializes transcription pipeline
3. Processes video/audio
4. Outputs transcription to stdout

**Key Files:**
- `src/trns/cli/main.py` - CLI entry point
- `src/trns/transcription/main.py` - Main transcription logic

### 2. Telegram Bot Interface (`trns.bot`)

**Purpose:** Interactive Telegram bot for transcription

**Architecture:**
- **FastAPI Server** (`trns.bot.server`): Webhook server
- **Route Handlers** (`trns.bot.routes`): Message and command handlers
- **Utilities** (`trns.bot.utils`): Authentication, token management, config
- **Output Handler** (`trns.bot.output_handler`): Real-time message sending

**Flow:**
1. User sends message (URL, video file, or command)
2. FastAPI receives webhook update
3. Route handler processes message
4. Transcription pipeline runs in background thread
5. Output sent to Telegram via queue-based system

**Key Features:**
- Webhook-based (no polling)
- Real-time transcription updates
- User authentication
- Daily capacity tracking
- Multi-user support

### 3. Transcription Pipeline (`trns.transcription.pipeline`)

**Purpose:** Orchestrates the entire transcription process

**Components:**
- **Subtitle Extractor** (`subtitle_extractor.py`): Extracts auto-generated subtitles
- **Whisper Transcriber** (`whisper_transcriber.py`): Speech-to-text using Whisper
- **Language Model Processor** (`language_model.py`): Processes transcriptions through LM

**Pipeline Flow:**

```
Input (URL/File)
    │
    ├─► Extract Audio (yt-dlp/FFmpeg)
    │
    ├─► Try Subtitles First (if available)
    │   └─► YouTube Subtitle Extractor
    │
    ├─► Fallback to Whisper (if no subtitles)
    │   └─► Whisper Transcriber
    │       ├─► Language Detection
    │       └─► Transcription
    │
    ├─► Translation (if not Russian)
    │   └─► Deep Translator
    │
    └─► Language Model Processing
        └─► OpenRouter.ai API
            └─► Generate Report
```

**Processing Modes:**
- **Auto**: Try subtitles first, fallback to Whisper
- **Subtitles Only**: Only use auto-generated subtitles
- **Whisper Only**: Only use speech-to-text

### 4. Audio Extraction (`trns.transcription.whisper_transcriber`)

**Purpose:** Extract audio from various sources

**Supported Sources:**
- YouTube videos (via yt-dlp)
- Twitter/X.com videos (via yt-dlp)
- Local video files (via FFmpeg)

**Process:**
1. Download/extract video
2. Extract audio track
3. Convert to format suitable for Whisper
4. Return audio file path

### 5. Whisper Transcription (`trns.transcription.whisper_transcriber`)

**Purpose:** Convert speech to text

**Features:**
- Automatic language detection
- Model size selection based on language
- Chunk-based processing for long videos
- Overlap handling to prevent text loss

**Models:**
- Uses `faster-whisper` for efficient processing
- Supports multiple model sizes (tiny, base, small, medium, large)

### 6. Language Model Processing (`trns.transcription.language_model`)

**Purpose:** Process transcriptions through language model for summaries/reports

**Provider:** OpenRouter.ai

**Features:**
- Daily capacity tracking (1000 requests/day)
- Automatic capacity reset at UTC midnight
- Token management
- Error handling with retries

**Process:**
1. Collect transcription chunks
2. Send to OpenRouter.ai API
3. Receive processed report
4. Return formatted output

## Data Flow

### CLI Flow

```
User: trns <url>
    │
    ├─► Parse arguments
    ├─► Initialize pipeline
    ├─► Process video
    │   ├─► Extract audio
    │   ├─► Transcribe
    │   ├─► Translate
    │   └─► LM processing
    └─► Output to stdout
```

### Telegram Bot Flow

```
User sends message
    │
    ├─► Telegram API
    ├─► Webhook → FastAPI
    ├─► Route Handler
    │   ├─► Authentication check
    │   ├─► Capacity check
    │   └─► Start processing (async)
    │
    ├─► Background Thread
    │   ├─► Transcription Pipeline
    │   │   ├─► Extract audio
    │   │   ├─► Transcribe
    │   │   ├─► Translate
    │   │   └─► LM processing
    │   │
    │   └─► Output Queue
    │       └─► Send to Telegram (real-time)
    │
    └─► User receives updates
```

## Configuration Management

### Environment Variables (Priority 1)

- `BOT_TOKEN`: Telegram bot token
- `AUTH_KEY`: Authentication key
- `OPENROUTER_API_KEY`: API key for LM processing
- `ALLOWED_USER_IDS`: Comma-separated user IDs
- `HOST`, `PORT`: Server configuration
- `CONFIG_PATH`, `METADATA_PATH`: File paths

### File-based Configuration (Priority 2)

- `bot_key.txt`: Bot token
- `key.txt`: Auth key
- `api_key.txt`: API keys (one per line)
- `allowed_ids.txt`: User IDs (one per line)
- `config.json`: Application config
- `metadata.json`: Localization

## State Management

### User States (Telegram Bot)

- `waiting_key`: User needs to authenticate
- `waiting_context`: User is setting context
- `processing`: User has active transcription

### Processing States

- In-memory task tracking per user
- Thread-safe locks for concurrent access
- Graceful shutdown support

## Threading Model

### Telegram Bot

- **Main Thread**: FastAPI/uvicorn server
- **Background Threads**: Transcription processing per user
- **Output Thread**: Queue-based message sending

### Transcription Pipeline

- **Main Thread**: Orchestration
- **Transcription Thread**: Audio processing
- **LM Thread**: Language model processing

## Error Handling

### Levels

1. **User-facing**: Friendly error messages via Telegram
2. **Logging**: Detailed errors in logs
3. **Retries**: Automatic retries for API calls
4. **Graceful Degradation**: Fallback methods (subtitles → Whisper)

### Error Types

- **Authentication Errors**: User not authorized
- **Capacity Errors**: Daily limit exceeded
- **Transcription Errors**: Audio extraction/processing failures
- **API Errors**: OpenRouter.ai failures (with retries)

## Security

### Authentication

- File-based or environment variable auth keys
- User ID whitelist
- Per-request authentication checks

### Token Management

- Secure token storage (files or env vars)
- Daily capacity limits
- Automatic reset at UTC midnight

## Scalability Considerations

### Current Limitations

- In-memory state (not suitable for multiple instances)
- Single-threaded per-user processing
- File-based configuration

### Future Improvements

- Redis for shared state
- Database for user management
- Distributed task queue (Celery/RQ)
- Horizontal scaling support

## Dependencies

### Core

- `youtube-transcript-api`: Subtitle extraction
- `yt-dlp`: Video/audio download
- `faster-whisper`: Speech-to-text
- `deep-translator`: Translation
- `openai`: OpenRouter.ai client

### Bot

- `python-telegram-bot`: Telegram API
- `fastapi`: Webhook server
- `uvicorn`: ASGI server

### Utilities

- `python-dotenv`: Environment variables
- `pydantic`: Configuration validation
- `tqdm`: Progress bars

## Deployment Architecture

### Recommended: Yandex Serverless Containers

```
Internet
    │
    ├─► Telegram API
    │   └─► Webhook → Yandex Serverless Container
    │       ├─► FastAPI Server
    │       ├─► Route Handlers
    │       └─► Background Processing
    │
    └─► User (CLI)
        └─► Local Installation
```

### Alternative: VM Deployment

```
VM (Yandex Compute Cloud)
    │
    ├─► Nginx (Reverse Proxy)
    ├─► Systemd Service
    │   └─► FastAPI Server
    └─► File System
        └─► Config Files
```

## Performance Characteristics

### Transcription Speed

- **Subtitles**: ~1-2 seconds (instant)
- **Whisper (base)**: ~0.5-1x real-time
- **Whisper (large)**: ~0.1-0.3x real-time

### Resource Usage

- **Memory**: 2-4GB (depending on model size)
- **CPU**: High during transcription
- **Disk**: Temporary audio files

### Bottlenecks

1. Audio extraction (network/disk I/O)
2. Whisper transcription (CPU-intensive)
3. LM processing (API latency)

## Monitoring

### Health Checks

- `/health` endpoint for container health
- Bot status verification
- Capacity monitoring

### Logging

- Structured logging (JSON format for cloud)
- Log levels: INFO, WARNING, ERROR
- Request/response logging

## Future Enhancements

1. **Database Integration**: Persistent user state
2. **Caching**: Redis for video info
3. **Queue System**: Celery for background tasks
4. **Multi-language Support**: More languages in metadata
5. **Admin Interface**: Web dashboard
6. **Analytics**: Usage statistics

