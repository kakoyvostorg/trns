"""
Tests for LMProcessor pure methods and mocked API calls.

All external I/O (file reads, OpenAI client) is patched out so tests are
fully offline.
"""

import math
import pytest
from unittest.mock import MagicMock, patch, mock_open


def make_lm_processor(**kwargs):
    """
    Construct an LMProcessor with __init__ side-effects patched away,
    then allow callers to set attributes directly.
    """
    from trns.transcription.language_model import LMProcessor

    with patch.object(LMProcessor, "_initialize_client", return_value=None), \
         patch.object(LMProcessor, "_load_prompts", return_value=None):
        proc = LMProcessor(**kwargs)

    # Set up reasonable defaults for tests that exercise public methods
    proc.client = MagicMock()
    proc.prompt_russian = "Обработай текст:\n"
    proc.prompt_original_template = "Process the text in {LANGUAGE}:\n"
    return proc


# ---------------------------------------------------------------------------
# _calculate_window_size
# ---------------------------------------------------------------------------

class TestCalculateWindowSize:
    def test_exact_division(self):
        proc = make_lm_processor(window_seconds=120, interval=30)
        assert proc._calculate_window_size() == 4  # 120/30

    def test_ceiling_applied(self):
        proc = make_lm_processor(window_seconds=100, interval=30)
        assert proc._calculate_window_size() == math.ceil(100 / 30)  # 4

    def test_window_smaller_than_interval(self):
        proc = make_lm_processor(window_seconds=10, interval=30)
        assert proc._calculate_window_size() == 1  # ceil(10/30) = 1

    def test_window_equals_interval(self):
        proc = make_lm_processor(window_seconds=30, interval=30)
        assert proc._calculate_window_size() == 1

    def test_large_window(self):
        proc = make_lm_processor(window_seconds=600, interval=30)
        assert proc._calculate_window_size() == 20


# ---------------------------------------------------------------------------
# _get_window_text
# ---------------------------------------------------------------------------

class TestGetWindowText:
    def _make_transcriptions(self, texts):
        return [{"text": t, "translated": t} for t in texts]

    def test_empty_list_returns_empty_string(self):
        proc = make_lm_processor(window_seconds=120, interval=30)
        assert proc._get_window_text([]) == ""

    def test_fewer_than_window_returns_all(self):
        proc = make_lm_processor(window_seconds=120, interval=30)  # window=4
        items = self._make_transcriptions(["a", "b"])
        result = proc._get_window_text(items)
        assert result == "a b"

    def test_exactly_window_size(self):
        proc = make_lm_processor(window_seconds=120, interval=30)  # window=4
        items = self._make_transcriptions(["a", "b", "c", "d"])
        result = proc._get_window_text(items)
        assert result == "a b c d"

    def test_more_than_window_returns_last_n(self):
        proc = make_lm_processor(window_seconds=60, interval=30)  # window=2
        items = self._make_transcriptions(["old1", "old2", "old3", "new1", "new2"])
        result = proc._get_window_text(items)
        assert result == "new1 new2"

    def test_blank_translated_entries_excluded(self):
        proc = make_lm_processor(window_seconds=120, interval=30)
        items = [
            {"text": "a", "translated": "hello"},
            {"text": "b", "translated": "   "},  # blank — should be excluded
            {"text": "c", "translated": "world"},
        ]
        result = proc._get_window_text(items)
        assert result == "hello world"

    def test_missing_translated_key_excluded(self):
        proc = make_lm_processor(window_seconds=120, interval=30)
        items = [
            {"text": "a", "translated": "hello"},
            {"text": "b"},  # no 'translated' key
        ]
        result = proc._get_window_text(items)
        assert result == "hello"


# ---------------------------------------------------------------------------
# _get_language_name
# ---------------------------------------------------------------------------

class TestGetLanguageName:
    def test_known_codes(self):
        proc = make_lm_processor()
        assert proc._get_language_name("en") == "English"
        assert proc._get_language_name("ru") == "Russian"
        assert proc._get_language_name("fr") == "French"
        assert proc._get_language_name("zh") == "Chinese"
        assert proc._get_language_name("ja") == "Japanese"
        assert proc._get_language_name("ar") == "Arabic"

    def test_unknown_code_returns_uppercased(self):
        proc = make_lm_processor()
        assert proc._get_language_name("xx") == "XX"
        assert proc._get_language_name("fa") == "FA"


# ---------------------------------------------------------------------------
# process_transcription_window — mode routing
# ---------------------------------------------------------------------------

class TestProcessTranscriptionWindow:
    def _make_transcriptions(self, n=3):
        return [
            {"text": f"original {i}", "translated": f"russian {i}"}
            for i in range(n)
        ]

    def test_russian_only_mode_single_call(self):
        proc = make_lm_processor(
            window_seconds=120, interval=30,
            use_bilingual=False, detected_language="en",
        )
        proc.process_russian_translation = MagicMock(return_value="отчёт")

        result = proc.process_transcription_window(self._make_transcriptions())

        proc.process_russian_translation.assert_called_once()
        assert result == "отчёт"

    def test_bilingual_non_russian_returns_tuple(self):
        proc = make_lm_processor(
            window_seconds=120, interval=30,
            use_bilingual=True, detected_language="en",
        )
        proc.process_original_language = MagicMock(return_value="english report")
        proc.process_russian_translation = MagicMock(return_value="russian report")

        result = proc.process_transcription_window(self._make_transcriptions())

        assert result == ("english report", "russian report")

    def test_bilingual_russian_input_uses_single_call(self):
        """Even in bilingual mode, if detected_language is 'ru', only Russian call is made."""
        proc = make_lm_processor(
            window_seconds=120, interval=30,
            use_bilingual=True, detected_language="ru",
        )
        proc.process_russian_translation = MagicMock(return_value="отчёт")
        proc.process_original_language = MagicMock()

        result = proc.process_transcription_window(self._make_transcriptions())

        proc.process_original_language.assert_not_called()
        assert result == "отчёт"

    def test_returns_none_when_no_text(self):
        proc = make_lm_processor(window_seconds=120, interval=30)
        # Transcriptions with blank translated text
        items = [{"text": "x", "translated": "   "}]
        result = proc.process_transcription_window(items)
        assert result is None

    def test_returns_none_when_empty_list(self):
        proc = make_lm_processor(window_seconds=120, interval=30)
        result = proc.process_transcription_window([])
        assert result is None

    def test_bilingual_fallback_to_russian_when_original_fails(self):
        proc = make_lm_processor(
            window_seconds=120, interval=30,
            use_bilingual=True, detected_language="fr",
        )
        proc.process_original_language = MagicMock(return_value=None)  # fails
        proc.process_russian_translation = MagicMock(return_value="отчёт")

        result = proc.process_transcription_window(self._make_transcriptions())
        assert result == "отчёт"

    def test_bilingual_returns_none_when_both_fail(self):
        proc = make_lm_processor(
            window_seconds=120, interval=30,
            use_bilingual=True, detected_language="fr",
        )
        proc.process_original_language = MagicMock(return_value=None)
        proc.process_russian_translation = MagicMock(return_value=None)

        result = proc.process_transcription_window(self._make_transcriptions())
        assert result is None


# ---------------------------------------------------------------------------
# process_russian_translation — mocked OpenAI call
# ---------------------------------------------------------------------------

class TestProcessRussianTranslation:
    def _mock_completion(self, content):
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.content = content
        return completion

    def test_returns_stripped_content(self):
        proc = make_lm_processor()
        proc.client.chat.completions.create.return_value = self._mock_completion("\nотчёт\n")

        with patch.object(proc, "_get_token_and_decrement", return_value="tok"):
            result = proc.process_russian_translation("some text")

        assert result == "отчёт"

    def test_returns_none_on_empty_response(self):
        proc = make_lm_processor()
        proc.client.chat.completions.create.return_value = self._mock_completion("")

        with patch.object(proc, "_get_token_and_decrement", return_value="tok"):
            result = proc.process_russian_translation("some text")

        assert result is None

    def test_returns_none_on_none_content(self):
        proc = make_lm_processor()
        proc.client.chat.completions.create.return_value = self._mock_completion(None)

        with patch.object(proc, "_get_token_and_decrement", return_value="tok"):
            result = proc.process_russian_translation("some text")

        assert result is None

    def test_returns_none_on_blank_input(self):
        proc = make_lm_processor()
        result = proc.process_russian_translation("   ")
        assert result is None
        proc.client.chat.completions.create.assert_not_called()

    def test_retries_on_429(self):
        proc = make_lm_processor()
        ok_completion = self._mock_completion("report")
        proc.client.chat.completions.create.side_effect = [
            Exception("429 too many requests"),
            ok_completion,
        ]

        with patch.object(proc, "_get_token_and_decrement", return_value="tok"), \
             patch("time.sleep"):
            result = proc.process_russian_translation("text")

        assert result == "report"
        assert proc.client.chat.completions.create.call_count == 2


# ---------------------------------------------------------------------------
# Daily capacity — real _get_token_and_decrement + utils patches
# ---------------------------------------------------------------------------

class TestDailyCapacityEnforcement:
    """When decrement_daily_capacity returns False, LM must not call the API."""

    def _mock_completion(self, content):
        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.content = content
        return completion

    def test_process_russian_no_api_when_capacity_exhausted(self):
        proc = make_lm_processor()
        proc.client.chat.completions.create.return_value = self._mock_completion("should not run")

        with patch("trns.bot.utils.get_current_token", return_value="fake-key"), \
             patch("trns.bot.utils.decrement_daily_capacity", return_value=False):
            result = proc.process_russian_translation("some text")

        assert result is None
        proc.client.chat.completions.create.assert_not_called()

    def test_process_original_language_no_api_when_capacity_exhausted(self):
        proc = make_lm_processor()
        proc.client.chat.completions.create.return_value = self._mock_completion("should not run")

        with patch("trns.bot.utils.get_current_token", return_value="fake-key"), \
             patch("trns.bot.utils.decrement_daily_capacity", return_value=False):
            result = proc.process_original_language("hello world", "en")

        assert result is None
        proc.client.chat.completions.create.assert_not_called()

    def test_get_token_and_decrement_returns_none_when_capacity_false(self):
        proc = make_lm_processor()
        with patch("trns.bot.utils.get_current_token", return_value="fake-key"), \
             patch("trns.bot.utils.decrement_daily_capacity", return_value=False):
            assert proc._get_token_and_decrement() is None
