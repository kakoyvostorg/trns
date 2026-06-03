"""
Tests for _serialize_window_with_timecodes and the LMProcessor wiring that
makes use of it when `timecode_in_summary` is enabled.

We don't actually call OpenRouter — we instantiate LMProcessor with a stubbed
prompt file on disk and exercise `_get_window_text` + `_load_prompts`.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from trns.transcription.language_model import (
    _TIMECODE_INSTRUCTION_EN,
    _TIMECODE_INSTRUCTION_RU,
    LMProcessor,
    _format_timecode_for_prompt,
    _serialize_window_with_timecodes,
)

# ---------------------------------------------------------------------------
# _format_timecode_for_prompt (mirror of pipeline._format_timecode)
# ---------------------------------------------------------------------------

class TestFormatTimecodeForPrompt:
    def test_basic_mmss(self):
        assert _format_timecode_for_prompt(0) == '[00:00]'
        assert _format_timecode_for_prompt(65) == '[01:05]'

    def test_auto_promotes_at_one_hour(self):
        assert _format_timecode_for_prompt(3600) == '[01:00:00]'

    def test_force_hours(self):
        assert _format_timecode_for_prompt(30, force_hours=True) == '[00:00:30]'

    def test_none_and_negative_treated_as_zero(self):
        assert _format_timecode_for_prompt(None) == '[00:00]'
        assert _format_timecode_for_prompt(-10) == '[00:00]'


# ---------------------------------------------------------------------------
# _serialize_window_with_timecodes
# ---------------------------------------------------------------------------

class TestSerializeWindow:
    def test_empty_window(self):
        assert _serialize_window_with_timecodes([]) == ''

    def test_single_dict_no_anchors_passthrough(self):
        ts = [{'translated': 'hello world'}]
        out = _serialize_window_with_timecodes(ts)
        assert out == 'hello world'

    def test_single_anchor_at_start(self):
        ts = [{
            'translated': 'hello world',
            'anchors_translated': [{'offset': 0, 'time': 5.0}],
        }]
        out = _serialize_window_with_timecodes(ts)
        # Marker at offset 0 appears before the text.
        assert out.startswith('[00:05]')
        assert 'hello world' in out

    def test_multiple_anchors_respect_min_interval(self):
        # Five anchors at 0,10,20,30,40s — min_interval=30 should keep 0 and 30 only.
        ts = [{
            'translated': 'A B C D E',
            'anchors_translated': [
                {'offset': 0, 'time': 0.0},
                {'offset': 2, 'time': 10.0},
                {'offset': 4, 'time': 20.0},
                {'offset': 6, 'time': 30.0},
                {'offset': 8, 'time': 40.0},
            ],
        }]
        out = _serialize_window_with_timecodes(ts, min_interval_seconds=30.0)
        # Exactly two markers: at 00:00 and 00:30.
        assert out.count('[00:00]') == 1
        assert out.count('[00:30]') == 1
        # 10s, 20s, 40s skipped.
        assert '[00:10]' not in out
        assert '[00:20]' not in out
        assert '[00:40]' not in out

    def test_min_interval_zero_emits_every_anchor(self):
        ts = [{
            'translated': 'A B C',
            'anchors_translated': [
                {'offset': 0, 'time': 0.0},
                {'offset': 2, 'time': 1.0},
                {'offset': 4, 'time': 2.0},
            ],
        }]
        out = _serialize_window_with_timecodes(ts, min_interval_seconds=0.0)
        assert '[00:00]' in out
        assert '[00:01]' in out
        assert '[00:02]' in out

    def test_spacing_holds_across_transcription_boundaries(self):
        # Two dicts. If the serializer reset state per dict, both would emit
        # their head anchors. The cross-boundary contract says the second
        # dict's first anchor must still respect min_interval vs. the last
        # marker emitted in the first dict.
        ts = [
            {
                'translated': 'first',
                'anchors_translated': [{'offset': 0, 'time': 0.0}],
            },
            {
                'translated': 'second',
                # 10s later — within min_interval=30 → no marker.
                'anchors_translated': [{'offset': 0, 'time': 10.0}],
            },
        ]
        out = _serialize_window_with_timecodes(ts, min_interval_seconds=30.0)
        assert '[00:00]' in out
        assert '[00:10]' not in out
        assert 'first' in out and 'second' in out

    def test_which_text_uses_text_and_anchors(self):
        ts = [{
            'text': 'hello',
            'translated': 'привет',
            'anchors': [{'offset': 0, 'time': 5.0}],
            'anchors_translated': [{'offset': 0, 'time': 99.0}],
        }]
        out = _serialize_window_with_timecodes(ts, which='text')
        assert '[00:05]' in out
        assert 'hello' in out
        # Did NOT pick up the translated anchors.
        assert '[01:39]' not in out

    def test_invalid_which_raises(self):
        with pytest.raises(ValueError):
            _serialize_window_with_timecodes([{}], which='bogus')

    def test_skips_empty_text_dicts(self):
        ts = [
            {'translated': '', 'anchors_translated': [{'offset': 0, 'time': 1.0}]},
            {'translated': 'real', 'anchors_translated': [{'offset': 0, 'time': 60.0}]},
        ]
        out = _serialize_window_with_timecodes(ts, min_interval_seconds=30.0)
        # The 1s anchor was on empty text so it's skipped entirely; only [01:00].
        assert '[01:00]' in out
        assert '[00:01]' not in out

    def test_malformed_anchor_offset_is_skipped(self):
        ts = [{
            'translated': 'hello',
            'anchors_translated': [
                {'offset': 999, 'time': 5.0},  # out of range
                {'offset': 0, 'time': 10.0},
            ],
        }]
        # Out-of-range anchor is ignored; the in-range one still emits.
        out = _serialize_window_with_timecodes(ts, min_interval_seconds=0.0)
        assert '[00:10]' in out
        assert '[00:05]' not in out

    def test_long_video_auto_hhmmss(self):
        ts = [{
            'translated': 'late',
            'anchors_translated': [{'offset': 0, 'time': 3700.0}],
        }]
        out = _serialize_window_with_timecodes(ts)
        assert '[01:01:40]' in out


# ---------------------------------------------------------------------------
# LMProcessor wiring: _load_prompts appends instruction; _get_window_text routes
# ---------------------------------------------------------------------------

def _make_lm_processor(timecode_in_summary: bool, **extra):
    """Build an LMProcessor with prompt files stubbed and OpenAI client patched."""
    tmp = tempfile.mkdtemp()
    ru = os.path.join(tmp, 'prompt.md')
    en = os.path.join(tmp, 'prompt_original.md')
    key = os.path.join(tmp, 'api_key.txt')
    with open(ru, 'w', encoding='utf-8') as f:
        f.write('RUSSIAN_BASE_PROMPT')
    with open(en, 'w', encoding='utf-8') as f:
        f.write('ENGLISH_BASE_PROMPT {LANGUAGE}')
    with open(key, 'w', encoding='utf-8') as f:
        f.write('sk-fake-token\n')

    with patch('openai.OpenAI') as mock_openai:
        mock_openai.return_value = MagicMock()
        lm = LMProcessor(
            api_key_file=key,
            prompt_file=ru,
            prompt_original_file=en,
            metadata_path=os.path.join(tmp, 'metadata.json'),
            timecode_in_summary=timecode_in_summary,
            **extra,
        )
    return lm


class TestLMProcessorWiring:
    def test_instruction_not_appended_when_disabled(self):
        lm = _make_lm_processor(timecode_in_summary=False)
        assert _TIMECODE_INSTRUCTION_RU not in lm.prompt_russian
        assert _TIMECODE_INSTRUCTION_EN not in lm.prompt_original_template

    def test_instruction_appended_when_enabled(self):
        lm = _make_lm_processor(timecode_in_summary=True)
        assert _TIMECODE_INSTRUCTION_RU in lm.prompt_russian
        assert _TIMECODE_INSTRUCTION_EN in lm.prompt_original_template
        # Base prompt still present.
        assert 'RUSSIAN_BASE_PROMPT' in lm.prompt_russian
        assert 'ENGLISH_BASE_PROMPT' in lm.prompt_original_template

    def test_get_window_text_legacy_path_ignores_anchors(self):
        lm = _make_lm_processor(timecode_in_summary=False)
        transcriptions = [{
            'translated': 'hello world',
            'anchors_translated': [{'offset': 0, 'time': 5.0}],
        }]
        out = lm._get_window_text(transcriptions)
        assert out == 'hello world'
        assert '[' not in out

    def test_get_window_text_timecode_path_injects_markers(self):
        lm = _make_lm_processor(timecode_in_summary=True, timecode_min_interval_seconds=0.0)
        transcriptions = [{
            'translated': 'hello world',
            'anchors_translated': [{'offset': 0, 'time': 5.0}],
        }]
        out = lm._get_window_text(transcriptions)
        assert '[00:05]' in out
        assert 'hello world' in out

    def test_get_window_text_which_text(self):
        lm = _make_lm_processor(timecode_in_summary=True, timecode_min_interval_seconds=0.0)
        transcriptions = [{
            'text': 'hello',
            'translated': 'привет',
            'anchors': [{'offset': 0, 'time': 5.0}],
            'anchors_translated': [{'offset': 0, 'time': 99.0}],
        }]
        out = lm._get_window_text(transcriptions, which='text')
        assert '[00:05]' in out
        assert 'hello' in out

    def test_reload_prompts_reappends_only_once_per_call(self):
        # _load_prompts appends to a fresh file read each call, so repeated
        # invocations (e.g. after language detection) don't accumulate.
        lm = _make_lm_processor(timecode_in_summary=True)
        before = lm.prompt_russian.count(_TIMECODE_INSTRUCTION_RU)
        lm._load_prompts()
        after = lm.prompt_russian.count(_TIMECODE_INSTRUCTION_RU)
        assert before == 1
        assert after == 1
