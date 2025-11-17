#!/usr/bin/env python3
"""
Entry point script for YouTube Live Transcription

This is a compatibility wrapper that imports and runs the main module.
For direct usage, you can also run: python -m youtube_live_transcription
"""

if __name__ == "__main__":
    from youtube_live_transcription.main import main
    main()
