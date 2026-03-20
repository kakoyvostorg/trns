"""
Integration tests — require network access, yt-dlp, and faster-whisper.

Run explicitly with:
    pytest -m integration -v

These tests are skipped in the normal unit test run.
The reference video is a 132-second Russian-language clip used to verify
the full download → transcribe pipeline works end-to-end.
"""

import os
import pytest

# Video used for all integration tests: short Russian-language clip (~132s)
REFERENCE_VIDEO_ID = "SqJCR7HNSSo"
REFERENCE_VIDEO_URL = f"https://www.youtube.com/watch?v={REFERENCE_VIDEO_ID}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transcriber():
    """Construct a real WhisperTranscriber (loads tiny model)."""
    from trns.transcription.whisper_transcriber import WhisperTranscriber
    return WhisperTranscriber(use_faster_whisper=True)


# ---------------------------------------------------------------------------
# YouTube metadata fetch
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_get_video_info_returns_metadata():
    """yt-dlp can fetch basic metadata for the reference video."""
    transcriber = _make_transcriber()
    info = transcriber._get_video_info(REFERENCE_VIDEO_ID)

    assert info is not None, "Expected video info dict, got None"
    assert "title" in info
    assert "duration" in info
    assert info["duration"] > 0


@pytest.mark.integration
def test_get_video_info_not_live():
    """Reference video is a VOD, not a live stream."""
    transcriber = _make_transcriber()
    info = transcriber._get_video_info(REFERENCE_VIDEO_ID)

    assert info is not None
    assert info.get("is_live") is not True


# ---------------------------------------------------------------------------
# Full audio download + transcription
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_extract_audio_downloads_file():
    """extract_audio_from_youtube returns a path to an existing WAV file."""
    transcriber = _make_transcriber()
    audio_path = transcriber.extract_audio_from_youtube(REFERENCE_VIDEO_ID)

    try:
        assert audio_path is not None, "Expected audio path, got None"
        assert os.path.exists(audio_path), f"Audio file not found: {audio_path}"
        assert os.path.getsize(audio_path) > 0, "Audio file is empty"
    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)


@pytest.mark.integration
def test_transcribe_detects_russian():
    """Whisper detects Russian on the reference video."""
    transcriber = _make_transcriber()
    audio_path = transcriber.extract_audio_from_youtube(REFERENCE_VIDEO_ID)

    assert audio_path is not None, "Audio download failed — cannot transcribe"

    text, language, prob = transcriber.transcribe_audio(audio_path)
    # transcribe_audio removes the file itself

    assert language == "ru", f"Expected 'ru', got '{language}'"
    assert prob > 0.5, f"Language probability too low: {prob}"
    assert len(text) > 20, f"Transcription too short: {text!r}"


@pytest.mark.integration
def test_transcription_contains_russian_characters():
    """Transcribed text contains Cyrillic characters."""
    transcriber = _make_transcriber()
    audio_path = transcriber.extract_audio_from_youtube(REFERENCE_VIDEO_ID)

    assert audio_path is not None, "Audio download failed"

    text, _, _ = transcriber.transcribe_audio(audio_path)

    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    assert cyrillic > 10, f"Expected Cyrillic characters in output, got: {text!r}"


# ---------------------------------------------------------------------------
# Subtitle availability
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_subtitle_check_does_not_raise():
    """check_subtitles_available completes without exception."""
    from trns.transcription.subtitle_extractor import YouTubeSubtitleExtractor

    extractor = YouTubeSubtitleExtractor(REFERENCE_VIDEO_ID)
    available, langs = extractor.check_subtitles_available()

    # We don't assert True/False — availability may vary — just that it runs
    assert isinstance(available, bool)
    assert isinstance(langs, list)


@pytest.mark.integration
def test_get_new_subtitles_returns_list():
    """get_new_subtitles returns a list (possibly empty) without crashing."""
    from trns.transcription.subtitle_extractor import YouTubeSubtitleExtractor

    extractor = YouTubeSubtitleExtractor(REFERENCE_VIDEO_ID)
    result = extractor.get_new_subtitles(language="ru")

    assert isinstance(result, list)
