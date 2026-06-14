"""
Provider-selection regressions for timecoded source handling.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from trns.transcription.pipeline import TranscriptionPipeline


def _args(**overrides):
    values = {
        "method": "auto",
        "interval": 30,
        "language": "en",
        "whisper_model": "tiny",
        "use_faster_whisper": True,
        "translation_output": "russian-only",
        "save_transcript": None,
        "overlap": 2,
        "process_mode": "auto",
        "lm_output_mode": "transcriptions-only",
        "timecode": True,
        "timecode_in_summary": True,
        "timecode_min_interval_seconds": 30.0,
        "yt_dlp_cookie_file": None,
        "yt_dlp_cookies_from_browser": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeTranscriber:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeTranscriber.instances.append(self)

    def _get_video_info(self, _video_id):
        return {
            "is_live": False,
            "duration": 40.0,
            "title": "title",
            "description": "",
            "chapters": [],
        }


class FakeSubtitleExtractor:
    def __init__(self, segments):
        self.segments = segments
        self.last_timestamp = 0.0
        self.calls = 0

    def check_subtitles_available(self):
        return True, ["en (auto-generated)"]

    def get_new_subtitles(self, language="en"):
        self.calls += 1
        self.last_timestamp = self.segments[-1]["start"] + self.segments[-1]["duration"]
        return self.segments


class TestPipelineProviderSelection:
    def setup_method(self):
        FakeTranscriber.instances = []

    def test_youtube_shorts_uses_youtube_subtitle_provider(self):
        fake_extractor = MagicMock()
        fake_extractor.check_subtitles_available.return_value = (True, ["en"])

        with patch(
            "trns.transcription.pipeline.YouTubeSubtitleExtractor",
            return_value=fake_extractor,
        ) as youtube_cls:
            with patch("trns.transcription.pipeline.WhisperTranscriber", FakeTranscriber):
                pipeline = TranscriptionPipeline(
                    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                    _args(),
                    lambda: False,
                )
                pipeline.initialize_components()

        youtube_cls.assert_called_once()
        assert pipeline.use_subtitles is True
        assert FakeTranscriber.instances == []

    def test_x_no_subtitles_falls_back_to_whisper_with_cookies(self):
        fake_extractor = MagicMock()
        fake_extractor.check_subtitles_available.return_value = (False, [])

        with patch(
            "trns.transcription.pipeline.YtDlpSubtitleExtractor",
            return_value=fake_extractor,
        ) as ytdlp_subtitle_cls:
            with patch("trns.transcription.pipeline.WhisperTranscriber", FakeTranscriber):
                pipeline = TranscriptionPipeline(
                    "https://x.com/user/status/123",
                    _args(
                        yt_dlp_cookie_file="/tmp/cookies.txt",
                        yt_dlp_cookies_from_browser="firefox",
                    ),
                    lambda: False,
                )
                pipeline.initialize_components()

        ytdlp_subtitle_cls.assert_called_once()
        assert pipeline.use_subtitles is False
        assert pipeline.use_whisper is True
        assert FakeTranscriber.instances[0].kwargs["yt_dlp_cookie_file"] == "/tmp/cookies.txt"
        assert FakeTranscriber.instances[0].kwargs["yt_dlp_cookies_from_browser"] == "firefox"

    def test_shorts_subtitles_translate_without_whisper_transcriber(self):
        segments = [
            {"start": float(i * 2), "duration": 1.0, "text": f"english {i}."}
            for i in range(55)
        ]
        fake_extractor = FakeSubtitleExtractor(segments)
        emitted = []

        def fake_translate(subtitle_segments, source_language):
            assert source_language == "en"
            translated = [
                {**seg, "text": seg["text"].replace("english", "russian")}
                for seg in subtitle_segments
            ]
            return " ".join(seg["text"] for seg in translated), translated

        with patch(
            "trns.transcription.pipeline.YouTubeSubtitleExtractor",
            return_value=fake_extractor,
        ):
            with patch("trns.transcription.pipeline.WhisperTranscriber", FakeTranscriber):
                with patch("trns.transcription.pipeline.translate_segments_to_russian", fake_translate):
                    pipeline = TranscriptionPipeline(
                        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                        _args(timecode=False),
                        lambda: False,
                        output_callback=emitted.append,
                    )
                    pipeline.run()

        joined = "".join(emitted)
        assert FakeTranscriber.instances == []
        assert "[en] english 0." in joined
        assert "russian 0." in joined
        assert pipeline.all_transcribed_text[0]["translated"].startswith("russian 0.")
