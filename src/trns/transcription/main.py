#!/usr/bin/env python3
"""
YouTube Live Stream Real-Time Transcription Script

This script extracts text from a YouTube live stream in real time using:
1. Auto-generated subtitles (if available) - checked every 30 seconds
2. Speech-to-text with Whisper (if subtitles unavailable) - processes audio in 30-second chunks
3. Language model processing for generating reports from transcriptions

Usage:
    python -m youtube_live_transcription <youtube_url> [--method METHOD] [--interval SECONDS]
    
    Methods:
        - auto (default): Try subtitles first, fallback to Whisper
        - subtitles: Only use subtitles
        - whisper: Only use Whisper
    
    Example:
        python -m youtube_live_transcription "https://www.youtube.com/watch?v=VIDEO_ID" --interval 30

Dependencies:
    pip install -r requirements.txt
"""

import argparse
import sys
import signal
import logging
import json
import os

from .pipeline import TranscriptionPipeline, extract_video_id

# Configure logging (will be reconfigured in main() based on debug mode)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Global flag for graceful shutdown
shutdown_flag = False


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    global shutdown_flag
    logger.info("\nReceived interrupt signal. Shutting down gracefully...")
    shutdown_flag = True


# Register signal handler
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def get_shutdown_flag():
    """Get current shutdown flag value"""
    global shutdown_flag
    return shutdown_flag


def create_default_config(config_path: str = "config.json"):
    """Create a default configuration JSON file with all parameters"""
    default_config = {
        "url": "",
        "method": "auto",
        "interval": 30,
        "language": "en",
        "whisper_model": "tiny",
        "use_faster_whisper": True,
        "translation_output": "russian-only",
        "save_transcript": None,
        "overlap": 2,
        "process_mode": "auto",
        "lm_window_seconds": 120,
        "lm_interval": 30,
        "lm_output_mode": "both",
        "lm_api_key_file": "api_key.txt",
        "lm_prompt_file": "prompt.md",
        "lm_model": "google/gemma-3-27b-it:free",
        "debug": False,
        "context": ""
    }
    
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(default_config, f, indent=2, ensure_ascii=False)
    
    return default_config


def load_config(config_path: str = "config.json"):
    """Load configuration from JSON file. Returns None if file doesn't exist."""
    if not os.path.exists(config_path):
        return None
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        return config
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing config.json: {e}")
        return None
    except Exception as e:
        logger.error(f"Error loading config.json: {e}")
        return None


def apply_config_to_args(args, config):
    """Apply configuration values to args, respecting 0 = default logic"""
    if config is None:
        return args
    
    # Map config keys to argument names (handle dashes vs underscores)
    config_map = {
        "method": "method",
        "interval": "interval",
        "language": "language",
        "whisper_model": "whisper_model",
        "use_faster_whisper": "use_faster_whisper",
        "translation_output": "translation_output",
        "save_transcript": "save_transcript",
        "overlap": "overlap",
        "process_mode": "process_mode",
        "lm_window_seconds": "lm_window_seconds",
        "lm_interval": "lm_interval",
        "lm_output_mode": "lm_output_mode",
        "lm_api_key_file": "lm_api_key_file",
        "lm_prompt_file": "lm_prompt_file",
        "lm_model": "lm_model",
        "debug": "debug",
        "context": "context"
    }
    
    # Numeric fields where 0 means use default
    numeric_fields = {"interval", "overlap", "lm_window_seconds", "lm_interval"}
    
    # Apply config values (0 means use default, so skip those)
    for config_key, arg_name in config_map.items():
        if config_key in config:
            value = config[config_key]
            
            # For numeric fields, 0 means use default
            if config_key in numeric_fields and isinstance(value, (int, float)) and value == 0:
                continue
            
            # For string fields, empty string means use default (except context and save_transcript)
            if isinstance(value, str) and value == "" and config_key not in {"context", "save_transcript", "url"}:
                continue
            
            # For None values, skip (means use default) except for save_transcript
            if value is None and config_key != "save_transcript":
                continue
            
            # Handle boolean values
            if config_key in {"use_faster_whisper", "debug"}:
                setattr(args, arg_name, bool(value))
            else:
                setattr(args, arg_name, value)
    
    return args


def main():
    """Main function to run the transcription script"""
    global shutdown_flag
    
    parser = argparse.ArgumentParser(
        description="Extract text from YouTube live stream in real time",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect method (subtitles first, then Whisper)
  python -m youtube_live_transcription "https://www.youtube.com/watch?v=VIDEO_ID"
  
  # Use only subtitles
  python -m youtube_live_transcription "https://www.youtube.com/watch?v=VIDEO_ID" --method subtitles
  
  # Use only Whisper
  python -m youtube_live_transcription "https://www.youtube.com/watch?v=VIDEO_ID" --method whisper
  
  # Custom interval
  python -m youtube_live_transcription "https://www.youtube.com/watch?v=VIDEO_ID" --interval 60
  
  # With LM processing (default: both transcriptions and reports)
  python -m youtube_live_transcription "https://www.youtube.com/watch?v=VIDEO_ID" --lm-output-mode both
        """
    )
    
    parser.add_argument(
        "url",
        nargs='?',  # Make URL optional (can come from config)
        help="YouTube video URL or video ID (can also be provided in config.json)"
    )
    parser.add_argument(
        "--method",
        choices=["auto", "subtitles", "whisper"],
        default="auto",
        help="Transcription method: auto (try subtitles first), subtitles only, or whisper only (default: auto)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Interval in seconds between transcription checks (default: 30)"
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Language code for subtitles (default: en)"
    )
    parser.add_argument(
        "--whisper-model",
        default="tiny",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper model size (default: tiny). Larger models are more accurate but slower."
    )
    parser.add_argument(
        "--use-faster-whisper",
        action="store_true",
        default=True,
        help="Use faster-whisper library (default: True, faster and more efficient)"
    )
    parser.add_argument(
        "--translation-output",
        choices=["russian-only", "both", "original-only"],
        default="russian-only",
        help="Translation output mode: russian-only (default), both (original + Russian), or original-only"
    )
    parser.add_argument(
        "--save-transcript",
        type=str,
        default=None,
        help="Optional file path to save cumulative transcript (e.g., transcript.txt)"
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=2,
        help="Overlap between chunks in seconds to prevent word loss (default: 2)"
    )
    parser.add_argument(
        "--process-mode",
        choices=["auto", "chunked", "full"],
        default="auto",
        help="Processing mode: auto (chunked for live, full for videos), chunked (process in chunks), or full (process entire video at once with progress bar) (default: auto)"
    )
    parser.add_argument(
        "--lm-window-seconds",
        type=int,
        default=120,
        help="Window size in seconds for LM processing (default: 120). LM will process last ceil(window_seconds/interval) transcriptions."
    )
    parser.add_argument(
        "--lm-interval",
        type=int,
        default=30,
        help="Interval in seconds between LM API calls (default: 30). Can be different from transcription interval."
    )
    parser.add_argument(
        "--lm-output-mode",
        choices=["transcriptions-only", "lm-only", "both"],
        default="both",
        help="Output mode: transcriptions-only (only transcriptions), lm-only (only LM reports), or both (default: both)"
    )
    parser.add_argument(
        "--lm-api-key-file",
        type=str,
        default="api_key.txt",
        help="Path to file containing OpenRouter.ai API key (default: api_key.txt)"
    )
    parser.add_argument(
        "--lm-prompt-file",
        type=str,
        default="prompt.md",
        help="Path to file containing the prompt for LM (default: prompt.md)"
    )
    parser.add_argument(
        "--lm-model",
        type=str,
        default="google/gemma-3-27b-it:free",
        help="OpenRouter.ai model name for LM processing (default: google/gemma-3-27b-it:free)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug mode (verbose logging to stdout). Default: production mode (logs to logs.txt, only outputs transcription/LM results)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="Path to configuration JSON file (default: config.json). If file doesn't exist, a default one will be created."
    )
    
    args = parser.parse_args()
    
    # Load configuration from JSON file (has priority over CLI args)
    config = load_config(args.config)
    if config is None:
        # Create default config file if it doesn't exist
        logger.info(f"Config file {args.config} not found. Creating default configuration...")
        create_default_config(args.config)
        logger.info(f"Default configuration created at {args.config}. You can edit it and run again.")
        config = load_config(args.config)
    
    # Store CLI URL before applying config (CLI URL has priority)
    cli_url = args.url
    
    # Apply config to args (config overrides CLI defaults)
    if config:
        args = apply_config_to_args(args, config)
        # Handle URL: CLI takes priority, then config
        if cli_url:
            args.url = cli_url  # CLI URL has priority
        elif not args.url and config.get("url"):
            args.url = config["url"]  # Use config URL only if CLI didn't provide one
    
    # Configure logging based on debug mode
    import logging.handlers
    
    # Remove existing handlers
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    if args.debug:
        # Debug mode: log everything to stdout
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        logger.info("Debug mode enabled - verbose logging to stdout")
    else:
        # Production mode: log to logs.txt, only INFO and above
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                'logs.txt',
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5,
                encoding='utf-8'
            )
            file_handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            root_logger.setLevel(logging.DEBUG)
            logger.info("Production mode enabled - logging to logs.txt")
        except Exception as e:
            # Fallback to stdout if file logging fails
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.INFO)
            formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
            handler.setFormatter(formatter)
            root_logger.addHandler(handler)
            root_logger.setLevel(logging.INFO)
            logger.warning(f"Failed to create logs.txt, falling back to stdout: {e}")
    
    # Extract video ID (URL can come from CLI or config)
    if not args.url:
        logger.error("YouTube URL or video ID is required. Provide it via --url argument or in config.json")
        sys.exit(1)
    
    try:
        video_id = extract_video_id(args.url)
        logger.info(f"Video ID: {video_id}")
    except Exception as e:
        logger.error(f"Invalid YouTube URL or video ID: {e}")
        sys.exit(1)
    
    # Create and run pipeline
    try:
        pipeline = TranscriptionPipeline(
            video_id=video_id,
            args=args,
            shutdown_flag=get_shutdown_flag
        )
        # Pass debug mode to pipeline
        pipeline.debug_mode = args.debug
        pipeline.run()
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        shutdown_flag = True
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        shutdown_flag = True
        sys.exit(1)


if __name__ == "__main__":
    main()

