"""
Tests for extract_video_id — the pure URL-parsing helper used by the pipeline.
"""

import pytest
from trns.transcription.pipeline import extract_video_id


class TestExtractVideoId:
    # --- Standard youtube.com URLs ---

    def test_standard_watch_url(self):
        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url_with_extra_params(self):
        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PLxxx") == "dQw4w9WgXcQ"

    def test_watch_url_params_before_v(self):
        assert extract_video_id("https://www.youtube.com/watch?list=PLxxx&v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_http_not_https(self):
        assert extract_video_id("http://www.youtube.com/watch?v=abc123XYZ") == "abc123XYZ"

    def test_no_www(self):
        assert extract_video_id("https://youtube.com/watch?v=abc123XYZ") == "abc123XYZ"

    # --- Short youtu.be URLs ---

    def test_short_url(self):
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url_with_timestamp(self):
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=30") == "dQw4w9WgXcQ"

    def test_short_url_with_si_param(self):
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?si=sometoken") == "dQw4w9WgXcQ"

    # --- Bare video ID (pass-through) ---

    def test_bare_video_id_unchanged(self):
        assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_bare_id_with_underscores(self):
        assert extract_video_id("abc_123-XYZ") == "abc_123-XYZ"

    # --- Non-YouTube URLs pass through unchanged ---

    def test_twitter_url_passthrough(self):
        url = "https://twitter.com/user/status/1234567890"
        assert extract_video_id(url) == url

    def test_x_com_url_passthrough(self):
        url = "https://x.com/user/status/1234567890"
        assert extract_video_id(url) == url

    def test_arbitrary_string_passthrough(self):
        assert extract_video_id("not-a-url") == "not-a-url"
