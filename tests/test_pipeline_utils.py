"""
Tests for extract_video_id — the pure URL-parsing helper used by the pipeline.
"""

from trns.transcription.pipeline import _next_non_live_chunk_position, extract_video_id


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

    # --- /live/ URLs ---

    def test_live_url(self):
        assert extract_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_live_url_with_query(self):
        assert extract_video_id("https://www.youtube.com/live/dQw4w9WgXcQ?si=token123") == "dQw4w9WgXcQ"

    def test_live_url_no_www(self):
        assert extract_video_id("https://youtube.com/live/abc_123-XYZ") == "abc_123-XYZ"

    # --- /embed/ URLs ---

    def test_embed_url(self):
        assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url_with_query(self):
        assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ?autoplay=1&rel=0") == "dQw4w9WgXcQ"

    def test_embed_url_no_www(self):
        assert extract_video_id("https://youtube.com/embed/abc_123-XYZ") == "abc_123-XYZ"

    # --- /shorts/ URLs ---

    def test_shorts_url(self):
        assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_shorts_url_with_query(self):
        assert extract_video_id("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share") == "dQw4w9WgXcQ"


class TestNextNonLiveChunkPosition:
    def test_regular_chunk_keeps_overlap(self):
        assert _next_non_live_chunk_position(0.0, 30.0, 2.0, 100.0) == 28.0

    def test_final_chunk_marks_video_complete(self):
        assert _next_non_live_chunk_position(38.5, 2.0, 2.0, 40.5) == 40.5

    def test_overlap_never_moves_cursor_backwards(self):
        assert _next_non_live_chunk_position(38.0, 1.0, 2.0, 100.0) == 38.0
