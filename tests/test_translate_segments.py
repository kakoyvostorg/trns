"""
Tests for WhisperTranscriber.translate_segments_to_russian — the
batch-with-separator segment translator that powers anchors_translated.

Uses a fake GoogleTranslator so the test suite stays offline.
"""

from unittest.mock import patch

# We can't instantiate WhisperTranscriber without loading a real model, so
# we instantiate a minimal-subclass stub that reuses only the method under test.
from trns.transcription.whisper_transcriber import WhisperTranscriber


def _make_transcriber():
    """Bypass model init — we only want translate_segments_to_russian."""
    t = WhisperTranscriber.__new__(WhisperTranscriber)
    t._translator_cache = {}
    t._last_detected_language = None
    return t


# ---------------------------------------------------------------------------
# Fake translators that simulate Google's behavior
# ---------------------------------------------------------------------------

class _EchoTranslator:
    """Returns input unchanged but uppercased so we can see each piece."""
    def __init__(self, *a, **kw): pass
    def translate(self, text):
        return text.upper()


class _LossyTranslator:
    """Loses some separators — simulates Google collapsing '|||' on short input."""
    def __init__(self, *a, **kw): pass
    def translate(self, text):
        # Drop the first occurrence of the separator to trigger mismatch
        return text.upper().replace('|||', '', 1)


class _PerSegmentTracker:
    """Count how many times translate() was called to verify batching."""
    def __init__(self, *a, **kw):
        self.calls = []
    def translate(self, text):
        self.calls.append(text)
        return text.upper()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTranslateSegmentsBasics:
    def test_empty_list_returns_empty(self):
        t = _make_transcriber()
        joined, translated = t.translate_segments_to_russian([], 'en')
        assert joined == ''
        assert translated == []

    def test_russian_source_is_passthrough(self):
        t = _make_transcriber()
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': 'Привет мир.'},
            {'start': 1.0, 'end': 2.0, 'text': 'Как дела?'},
        ]
        joined, translated = t.translate_segments_to_russian(segs, 'ru')
        # No translator should be called at all — all we check is text passes through
        assert len(translated) == 2
        assert translated[0]['text'] == 'Привет мир.'
        assert translated[1]['text'] == 'Как дела?'
        # Times preserved
        assert translated[0]['start'] == 0.0
        assert translated[1]['start'] == 1.0

    def test_successful_batch_preserves_alignment(self):
        t = _make_transcriber()
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': 'hello'},
            {'start': 1.0, 'end': 2.0, 'text': 'world'},
            {'start': 2.0, 'end': 3.0, 'text': 'foo'},
        ]
        with patch('deep_translator.GoogleTranslator', _EchoTranslator):
            joined, translated = t.translate_segments_to_russian(segs, 'en')
        assert len(translated) == 3
        assert translated[0]['text'] == 'HELLO'
        assert translated[1]['text'] == 'WORLD'
        assert translated[2]['text'] == 'FOO'
        # Times untouched
        for orig, tr in zip(segs, translated):
            assert tr['start'] == orig['start']
            assert tr['end'] == orig['end']
        # Joined matches " ".join(...).strip()
        assert joined == 'HELLO WORLD FOO'


class TestTranslateSegmentsBatching:
    def test_default_batch_size_yields_single_call_for_small_input(self):
        t = _make_transcriber()
        segs = [{'start': float(i), 'end': float(i + 1), 'text': f'w{i}'} for i in range(10)]
        tracker_instance = _PerSegmentTracker()
        with patch('deep_translator.GoogleTranslator', return_value=tracker_instance):
            t.translate_segments_to_russian(segs, 'en', batch_size=10)
        # 10 segments, batch_size=10 → exactly 1 batch call
        assert len(tracker_instance.calls) == 1
        # And that call was the joined input with 9 separators
        assert tracker_instance.calls[0].count('|||') == 9

    def test_multiple_batches_produce_multiple_calls(self):
        t = _make_transcriber()
        segs = [{'start': float(i), 'end': float(i + 1), 'text': f'w{i}'} for i in range(25)]
        tracker_instance = _PerSegmentTracker()
        with patch('deep_translator.GoogleTranslator', return_value=tracker_instance):
            t.translate_segments_to_russian(segs, 'en', batch_size=10)
        # 25 segments / 10 per batch → 3 calls (10 + 10 + 5)
        assert len(tracker_instance.calls) == 3


class TestTranslateSegmentsFallback:
    def test_separator_loss_triggers_per_segment_fallback(self):
        """If Google drops the separator we must still return the right count."""
        t = _make_transcriber()
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': 'alpha'},
            {'start': 1.0, 'end': 2.0, 'text': 'beta'},
            {'start': 2.0, 'end': 3.0, 'text': 'gamma'},
        ]
        with patch('deep_translator.GoogleTranslator', _LossyTranslator):
            joined, translated = t.translate_segments_to_russian(segs, 'en')
        # Must return exactly 3 segments despite the lossy batch
        assert len(translated) == 3
        # Per-segment fallback results
        assert translated[0]['text'] == 'ALPHA'
        assert translated[1]['text'] == 'BETA'
        assert translated[2]['text'] == 'GAMMA'

    def test_defensive_stripping_of_literal_separator_in_source(self):
        """Source text containing '|||' must not break the split."""
        t = _make_transcriber()
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': 'before|||after'},
            {'start': 1.0, 'end': 2.0, 'text': 'normal'},
        ]
        with patch('deep_translator.GoogleTranslator', _EchoTranslator):
            joined, translated = t.translate_segments_to_russian(segs, 'en')
        # 2 segments back, no splitting confusion
        assert len(translated) == 2
        # The |||-looking token was neutralized before translation
        assert '|||' not in translated[0]['text']

    def test_translator_exception_falls_back_per_segment(self):
        """Exception during batch call: per-segment fallback should still try."""
        t = _make_transcriber()

        class _FlakyTranslator:
            def __init__(self, *a, **kw):
                self.call = 0
            def translate(self, text):
                self.call += 1
                if self.call == 1:
                    # First call (the batch) fails
                    raise RuntimeError("transient")
                return text.upper()

        segs = [
            {'start': 0.0, 'end': 1.0, 'text': 'alpha'},
            {'start': 1.0, 'end': 2.0, 'text': 'beta'},
        ]
        with patch('deep_translator.GoogleTranslator', _FlakyTranslator):
            joined, translated = t.translate_segments_to_russian(segs, 'en')
        assert len(translated) == 2
        assert translated[0]['text'] == 'ALPHA'
        assert translated[1]['text'] == 'BETA'


class TestTranslateSegmentsNoDepTranslator:
    def test_import_error_returns_passthrough(self):
        """Missing deep_translator degrades gracefully."""
        t = _make_transcriber()
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': 'hello'},
            {'start': 1.0, 'end': 2.0, 'text': 'world'},
        ]
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == 'deep_translator':
                raise ImportError("simulated")
            return real_import(name, *a, **kw)

        with patch('builtins.__import__', side_effect=fake_import):
            joined, translated = t.translate_segments_to_russian(segs, 'en')
        # Passthrough: original text survives
        assert len(translated) == 2
        assert translated[0]['text'] == 'hello'
        assert translated[1]['text'] == 'world'
