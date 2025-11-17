#!/usr/bin/env python3
"""
CLI Entry Point for TRNS

Entry point for the transcription CLI tool.
Can be invoked as: trns <url>
"""

def main():
    """CLI entry point"""
    from trns.transcription.main import main as transcription_main
    transcription_main()


if __name__ == "__main__":
    main()
