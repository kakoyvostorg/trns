"""Dev utility: verify anchors are produced on a real video.

Usage:
    python scripts/dev_check_anchors.py
    python scripts/dev_check_anchors.py --url https://www.youtube.com/watch?v=...

This intentionally hits YouTube/yt-dlp/Whisper. It is not part of the default
test suite; use it as a manual smoke check when changing anchor/timecode code.
"""

import argparse

from trns.transcription.pipeline import TranscriptionPipeline, extract_video_id


def main():
    parser = argparse.ArgumentParser(description="Manual anchor/timecode smoke check")
    parser.add_argument(
        "--url",
        default="https://www.youtube.com/watch?v=jNQXAC9IVRw",
        help='Video URL to process; defaults to the short "Me at the zoo" video.',
    )
    args_in = parser.parse_args()

    args = argparse.Namespace(
        url=args_in.url,
        method="whisper",
        process_mode="full",
        interval=30,
        overlap=2,
        language="en",
        whisper_model="tiny",
        use_faster_whisper=True,
        translation_output="russian-only",
        save_transcript=None,
        lm_window_seconds=120,
        lm_interval=30,
        lm_output_mode="transcriptions-only",
        lm_api_key_file="api_key.txt",
        lm_prompt_file="prompt.md",
        lm_prompt_original_file="prompt_original.md",
        lm_model="google/gemma-3-27b-it:free",
        debug=True,
        context="",
        show_original_translation=True,
        show_transcription=True,
        timecode=True,
        timecode_in_summary=False,
        video_metadata_chars=0,
    )

    pipeline = TranscriptionPipeline(
        extract_video_id(args.url),
        args,
        shutdown_flag=lambda: False,
    )
    pipeline.initialize_components()
    pipeline.run()

    for i, entry in enumerate(pipeline.all_transcribed_text):
        print(f"\n--- entry {i} ---")
        print(f"text[:80]: {entry['text'][:80]!r}")
        anchors = entry.get("anchors", [])
        print(f"anchors: {len(anchors)}")
        for anchor in anchors[:3]:
            snippet = entry["text"][anchor["offset"]:anchor["offset"] + 40]
            print(f"  [{anchor['time']:.2f}s] @offset {anchor['offset']}: {snippet!r}")


if __name__ == "__main__":
    main()
