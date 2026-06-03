"""
Regression test for the subtitle path wiring introduced in commit 7:
_build_anchors accepts subtitle-shaped dicts ({'text', 'start', 'duration'})
without needing 'end', and produces anchors whose times match each segment's
`start` (subtitles are already video-absolute).
"""

from trns.transcription.pipeline import _build_anchors


class TestSubtitleAnchors:
    def test_subtitle_dicts_accepted(self):
        # Subtitle shape: no 'end' key, only 'duration'.
        segments = [
            {'text': 'Hello there.', 'start': 0.0, 'duration': 2.5},
            {'text': 'How are you?', 'start': 2.5, 'duration': 2.0},
            {'text': 'Goodbye.', 'start': 4.5, 'duration': 1.5},
        ]
        text, anchors = _build_anchors(segments, chunk_start=0.0)
        assert 'Hello there.' in text
        assert 'How are you?' in text
        assert 'Goodbye.' in text
        assert len(anchors) == 3
        # Times come straight from the segments (chunk_start=0).
        assert anchors[0]['time'] == 0.0
        assert anchors[1]['time'] == 2.5
        assert anchors[2]['time'] == 4.5

    def test_subtitle_anchor_offsets_align_to_text(self):
        segments = [
            {'text': 'Alpha', 'start': 0.0, 'duration': 1.0},
            {'text': 'Beta', 'start': 1.0, 'duration': 1.0},
        ]
        text, anchors = _build_anchors(segments)
        # The rebuilt text uses the pipeline's " ".join(...).strip() contract.
        assert text == 'Alpha Beta'
        # Offsets point to the start of each segment's text inside `text`.
        assert text[anchors[0]['offset']:].startswith('Alpha')
        assert text[anchors[1]['offset']:].startswith('Beta')

    def test_empty_subtitle_segment_skipped(self):
        segments = [
            {'text': 'real', 'start': 0.0, 'duration': 1.0},
            {'text': '', 'start': 5.0, 'duration': 1.0},
            {'text': 'more', 'start': 10.0, 'duration': 1.0},
        ]
        text, anchors = _build_anchors(segments)
        # Empty segment contributes nothing to text or anchors.
        assert text == 'real  more'  # " ".join keeps the empty separator
        assert [a['time'] for a in anchors] == [0.0, 10.0]
