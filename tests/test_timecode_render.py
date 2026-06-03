"""
Tests for _format_timecode and for the timecode-prefix behaviour that
_output_transcription attaches when `self.timecode_mode` is on.

We avoid instantiating a full TranscriptionPipeline (that needs a model +
downloader); instead we new() a bare object and attach only the attributes the
method reads. This keeps tests fast and offline.
"""

import types

from trns.transcription.pipeline import TranscriptionPipeline, _format_timecode

# ---------------------------------------------------------------------------
# _format_timecode
# ---------------------------------------------------------------------------

class TestFormatTimecode:
    def test_zero(self):
        assert _format_timecode(0) == '[00:00]'

    def test_small_mm_ss(self):
        assert _format_timecode(65) == '[01:05]'
        assert _format_timecode(65.9) == '[01:05]'  # truncates, no rounding up

    def test_under_one_hour_stays_mm_ss(self):
        assert _format_timecode(3599) == '[59:59]'

    def test_over_one_hour_auto_adds_hours(self):
        assert _format_timecode(3600) == '[01:00:00]'
        assert _format_timecode(3661) == '[01:01:01]'

    def test_force_hours_true_always_hhmmss(self):
        assert _format_timecode(59, force_hours=True) == '[00:00:59]'
        assert _format_timecode(0, force_hours=True) == '[00:00:00]'

    def test_force_hours_false_never_hhmmss(self):
        # Large total with force_hours=False: minutes just keep going
        assert _format_timecode(3599, force_hours=False) == '[59:59]'

    def test_negative_clamped_to_zero(self):
        assert _format_timecode(-5) == '[00:00]'

    def test_none_treated_as_zero(self):
        assert _format_timecode(None) == '[00:00]'

    def test_non_numeric_string_falls_back_to_zero(self):
        assert _format_timecode("garbage") == '[00:00]'


# ---------------------------------------------------------------------------
# _output_transcription prefix wiring
# ---------------------------------------------------------------------------

def _make_stub_pipeline(timecode_mode: bool, video_duration=None):
    """
    Build a bare TranscriptionPipeline without triggering __init__ so we can
    test _output_transcription in isolation.
    """
    p = TranscriptionPipeline.__new__(TranscriptionPipeline)
    p.timecode_mode = timecode_mode
    p.video_duration = video_duration
    p.show_transcription = True
    p.show_original_translation = False
    p.detected_language = "ru"
    p.lm_processor = None
    p.debug_mode = False
    # args: anything .get-like used by the method
    args = types.SimpleNamespace(
        lm_output_mode='both',
        save_transcript=None,
        translation_output='russian-only',
    )
    p.args = args
    # Collect outputs instead of printing
    p._emitted = []
    p._output_callback = p._emitted.append
    return p


class TestOutputTranscriptionTimecodePrefix:
    def test_prefix_absent_when_mode_off(self):
        p = _make_stub_pipeline(timecode_mode=False)
        p._output_transcription('ts', 'hello', 'привет', 'en', 0.99, timecode=65.0)
        joined = ''.join(p._emitted)
        assert '[01:05]' not in joined
        assert 'привет' in joined

    def test_prefix_present_when_mode_on(self):
        p = _make_stub_pipeline(timecode_mode=True)
        p._output_transcription('ts', 'hello', 'привет', 'en', 0.99, timecode=65.0)
        joined = ''.join(p._emitted)
        assert '[01:05]' in joined
        assert 'привет' in joined

    def test_prefix_absent_when_timecode_none(self):
        p = _make_stub_pipeline(timecode_mode=True)
        p._output_transcription('ts', 'hello', 'привет', 'en', 0.99, timecode=None)
        joined = ''.join(p._emitted)
        # No leading "[..:..]" anywhere
        assert '[00:00]' not in joined
        assert 'привет' in joined

    def test_long_video_forces_hhmmss(self):
        p = _make_stub_pipeline(timecode_mode=True, video_duration=7200)  # 2h
        p._output_transcription('ts', 'hello', 'привет', 'en', 0.99, timecode=30.0)
        joined = ''.join(p._emitted)
        # 30s on a 2h video → force HH:MM:SS so the stream is uniform
        assert '[00:00:30]' in joined

    def test_short_video_stays_mmss(self):
        p = _make_stub_pipeline(timecode_mode=True, video_duration=120)  # 2min
        p._output_transcription('ts', 'hello', 'привет', 'en', 0.99, timecode=30.0)
        joined = ''.join(p._emitted)
        assert '[00:30]' in joined
        assert '[00:00:30]' not in joined

    def test_show_original_translation_both_lines_prefixed(self):
        p = _make_stub_pipeline(timecode_mode=True)
        p.show_original_translation = True
        p._output_transcription('ts', 'hello', 'привет', 'en', 0.99, timecode=65.0)
        joined = ''.join(p._emitted)
        # Both lines should carry the prefix
        assert joined.count('[01:05]') >= 2
