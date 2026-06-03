"""
Tests for _build_anchors — the helper that pairs a transcription string with a
parallel list of (offset, video-absolute time) markers.
"""

import pytest

from trns.transcription.pipeline import _build_anchors


# Simple object that mimics faster-whisper's Segment (has .start / .text attrs)
class _Seg:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class TestBuildAnchorsDictInput:
    def test_empty_returns_empty(self):
        text, anchors = _build_anchors([])
        assert text == ''
        assert anchors == []

    def test_none_returns_empty(self):
        text, anchors = _build_anchors(None)
        assert text == ''
        assert anchors == []

    def test_single_segment(self):
        segs = [{'start': 0.0, 'end': 1.5, 'text': ' Hello world.'}]
        text, anchors = _build_anchors(segs)
        assert text == 'Hello world.'
        assert anchors == [{'offset': 0, 'time': 0.0}]

    def test_multiple_segments_produce_same_text_as_pipeline(self):
        """Text should equal ' '.join(seg.text for seg in segs).strip() — the
        existing pipeline contract."""
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': ' Hello world.'},
            {'start': 1.0, 'end': 2.5, 'text': ' This is a test.'},
            {'start': 2.5, 'end': 4.0, 'text': ' Third sentence here.'},
        ]
        text, anchors = _build_anchors(segs)
        expected = ' '.join(s['text'] for s in segs).strip()
        assert text == expected
        assert len(anchors) == 3

    def test_anchor_offsets_point_at_segment_starts(self):
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': ' Hello world.'},
            {'start': 1.0, 'end': 2.5, 'text': ' This is a test.'},
            {'start': 2.5, 'end': 4.0, 'text': ' Third sentence here.'},
        ]
        text, anchors = _build_anchors(segs)
        # Each anchor's offset should be where its stripped segment text begins
        assert text[anchors[0]['offset']:].startswith('Hello world.')
        assert text[anchors[1]['offset']:].startswith('This is a test.')
        assert text[anchors[2]['offset']:].startswith('Third sentence here.')

    def test_chunk_start_is_added_to_anchor_time(self):
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': ' Hello.'},
            {'start': 1.0, 'end': 2.0, 'text': ' World.'},
        ]
        _, anchors = _build_anchors(segs, chunk_start=120.5)
        assert anchors[0]['time'] == pytest.approx(120.5)
        assert anchors[1]['time'] == pytest.approx(121.5)

    def test_repeated_segment_text_does_not_alias(self):
        """If two segments contain the exact same text, their anchors must
        point at distinct occurrences (sequential forward search)."""
        segs = [
            {'start': 0.0, 'end': 1.0, 'text': ' Hello.'},
            {'start': 1.0, 'end': 2.0, 'text': ' Hello.'},
            {'start': 2.0, 'end': 3.0, 'text': ' Goodbye.'},
        ]
        text, anchors = _build_anchors(segs)
        assert anchors[0]['offset'] < anchors[1]['offset']
        assert text[anchors[0]['offset']:].startswith('Hello.')
        assert text[anchors[1]['offset']:].startswith('Hello.')
        # Second anchor must be past first occurrence's end
        assert anchors[1]['offset'] >= anchors[0]['offset'] + len('Hello.')

    def test_empty_segment_text_is_skipped_in_anchors(self):
        segs = [
            {'start': 0.0, 'end': 0.5, 'text': ''},
            {'start': 0.5, 'end': 1.0, 'text': ' Hello.'},
        ]
        text, anchors = _build_anchors(segs)
        assert text == 'Hello.'
        assert len(anchors) == 1
        assert anchors[0]['time'] == pytest.approx(0.5)


class TestBuildAnchorsObjectInput:
    """Helper must also accept faster-whisper native Segment-like objects."""

    def test_object_segments(self):
        segs = [
            _Seg(0.0, 1.0, ' Hello.'),
            _Seg(1.0, 2.0, ' World.'),
        ]
        text, anchors = _build_anchors(segs, chunk_start=10.0)
        # Matches existing pipeline's " ".join(...).strip() behavior: the
        # leading spaces inside each segment produce a double-space between
        # segments. This is the contract we must preserve.
        assert text == 'Hello.  World.'
        assert anchors[0]['time'] == pytest.approx(10.0)
        assert anchors[1]['time'] == pytest.approx(11.0)
        # Anchors still point at the correct chars regardless of double-space
        assert text[anchors[0]['offset']:].startswith('Hello.')
        assert text[anchors[1]['offset']:].startswith('World.')


class TestBuildAnchorsAbsoluteTime:
    """Anchors must always carry video-absolute seconds (chunk-relative time
    from whisper + chunk_start offset)."""

    def test_live_stream_relative_to_capture_start(self):
        # Second chunk in a live stream: chunk_start = 28 (30s interval - 2s overlap)
        segs = [{'start': 0.0, 'end': 1.0, 'text': ' Next line.'}]
        _, anchors = _build_anchors(segs, chunk_start=28.0)
        assert anchors[0]['time'] == pytest.approx(28.0)

    def test_non_live_chunked_second_chunk(self):
        segs = [{'start': 0.0, 'end': 1.0, 'text': ' Next chunk.'}]
        _, anchors = _build_anchors(segs, chunk_start=30.0)
        assert anchors[0]['time'] == pytest.approx(30.0)
