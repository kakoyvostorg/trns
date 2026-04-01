"""
Tests for YouTubeSubtitleExtractor.

YouTube API calls are fully mocked — no network access.
"""

import pytest
from unittest.mock import MagicMock, patch

from trns.transcription.subtitle_extractor import YouTubeSubtitleExtractor


# ---------------------------------------------------------------------------
# extract_video_id (instance method — same logic as the pipeline helper)
# ---------------------------------------------------------------------------

class TestExtractVideoId:
    def setup_method(self):
        self.extractor = YouTubeSubtitleExtractor("dummy_id")

    def test_standard_watch_url(self):
        result = self.extractor.extract_video_id("https://www.youtube.com/watch?v=abc123")
        assert result == "abc123"

    def test_watch_url_strips_extra_params(self):
        result = self.extractor.extract_video_id(
            "https://www.youtube.com/watch?v=abc123&t=30s"
        )
        assert result == "abc123"

    def test_short_youtu_be_url(self):
        result = self.extractor.extract_video_id("https://youtu.be/abc123")
        assert result == "abc123"

    def test_short_url_with_query_params(self):
        result = self.extractor.extract_video_id("https://youtu.be/abc123?t=10")
        assert result == "abc123"

    def test_bare_id_passthrough(self):
        result = self.extractor.extract_video_id("abc123XYZ_-")
        assert result == "abc123XYZ_-"

    def test_shorts_trailing_slash_returns_empty(self):
        result = self.extractor.extract_video_id("https://youtube.com/shorts/")
        assert result == ""

    def test_live_url(self):
        result = self.extractor.extract_video_id("https://www.youtube.com/live/abc123")
        assert result == "abc123"

    def test_embed_url(self):
        result = self.extractor.extract_video_id("https://www.youtube.com/embed/abc123")
        assert result == "abc123"


# ---------------------------------------------------------------------------
# get_new_subtitles — mocked YouTube Transcript API
# ---------------------------------------------------------------------------

FAKE_TRANSCRIPT = [
    {"text": "Hello world", "start": 0.0, "duration": 2.5},
    {"text": "How are you", "start": 2.5, "duration": 3.0},
    {"text": "I am fine",   "start": 5.5, "duration": 2.0},
]


class TestGetNewSubtitles:
    def _make_extractor(self, video_id="test_video"):
        return YouTubeSubtitleExtractor(video_id)

    def _patch_transcript_api(self, transcript=None, side_effect=None):
        """Return a context manager that patches YouTubeTranscriptApi.get_transcript.

        The class is imported inside the method body, so we patch it at its
        source module rather than the subtitle_extractor namespace.
        """
        mock_api = MagicMock()
        if side_effect:
            mock_api.get_transcript.side_effect = side_effect
        else:
            mock_api.get_transcript.return_value = transcript or FAKE_TRANSCRIPT

        return patch(
            "youtube_transcript_api.YouTubeTranscriptApi",
            mock_api,
        )

    def test_first_call_returns_all_segments(self):
        extractor = self._make_extractor()
        with self._patch_transcript_api(FAKE_TRANSCRIPT):
            result = extractor.get_new_subtitles(language="en")
        assert result == FAKE_TRANSCRIPT

    def test_last_timestamp_updated_after_call(self):
        extractor = self._make_extractor()
        with self._patch_transcript_api(FAKE_TRANSCRIPT):
            extractor.get_new_subtitles(language="en")
        # last segment: start=5.5, duration=2.0 -> timestamp = 7.5
        assert extractor.last_timestamp == pytest.approx(7.5)

    def test_second_call_returns_only_new_segments(self):
        extractor = self._make_extractor()
        # Simulate first call that consumed up to timestamp 2.5
        extractor.last_timestamp = 2.5

        with self._patch_transcript_api(FAKE_TRANSCRIPT):
            result = extractor.get_new_subtitles(language="en")

        # Only segments with start >= 2.5
        assert all(seg["start"] >= 2.5 for seg in result)
        assert len(result) == 2

    def test_returns_empty_list_on_transcripts_disabled(self):
        from youtube_transcript_api._errors import TranscriptsDisabled

        extractor = self._make_extractor()
        with self._patch_transcript_api(side_effect=TranscriptsDisabled("vid")):
            result = extractor.get_new_subtitles(language="en")
        assert result == []

    def test_returns_empty_list_on_no_transcript_found(self):
        from youtube_transcript_api._errors import NoTranscriptFound

        extractor = self._make_extractor()
        # Patch both get_transcript and list_transcripts to raise
        mock_api = MagicMock()
        mock_api.get_transcript.side_effect = NoTranscriptFound("vid", ["en"], {})
        mock_api.list_transcripts.side_effect = NoTranscriptFound("vid", ["en"], {})

        with patch("youtube_transcript_api.YouTubeTranscriptApi", mock_api):
            result = extractor.get_new_subtitles(language="en")
        assert result == []

    def test_returns_empty_list_on_generic_exception(self):
        extractor = self._make_extractor()
        with self._patch_transcript_api(side_effect=RuntimeError("network error")):
            result = extractor.get_new_subtitles(language="en")
        assert result == []

    def test_empty_transcript_leaves_timestamp_unchanged(self):
        extractor = self._make_extractor()
        extractor.last_timestamp = 10.0

        with self._patch_transcript_api([]):
            result = extractor.get_new_subtitles(language="en")

        assert result == []
        assert extractor.last_timestamp == 10.0  # unchanged


# ---------------------------------------------------------------------------
# check_subtitles_available — mocked
# ---------------------------------------------------------------------------

class TestCheckSubtitlesAvailable:
    def test_returns_true_with_languages_when_available(self):
        extractor = YouTubeSubtitleExtractor("test_id")

        mock_transcript = MagicMock()
        mock_transcript.is_generated = True
        mock_transcript.language_code = "en"

        mock_list = MagicMock()
        mock_list.__iter__ = MagicMock(return_value=iter([mock_transcript]))

        mock_api = MagicMock()
        mock_api.list_transcripts.return_value = mock_list

        with patch("youtube_transcript_api.YouTubeTranscriptApi", mock_api):
            available, langs = extractor.check_subtitles_available()

        assert available is True
        assert any("en" in lang for lang in langs)

    def test_returns_false_when_disabled(self):
        from youtube_transcript_api._errors import TranscriptsDisabled

        extractor = YouTubeSubtitleExtractor("test_id")
        mock_api = MagicMock()
        mock_api.list_transcripts.side_effect = TranscriptsDisabled("test_id")

        with patch("youtube_transcript_api.YouTubeTranscriptApi", mock_api):
            available, langs = extractor.check_subtitles_available()

        assert available is False
        assert langs == []
