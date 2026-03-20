"""
Tests for config loading and apply_config_to_args (trns.transcription.main).
"""

import json
import pytest
from trns.transcription.main import apply_config_to_args, load_config, create_default_config


class TestApplyConfigToArgs:
    """apply_config_to_args merges a JSON config dict into an argparse Namespace."""

    def test_method_overridden_by_config(self, minimal_args):
        config = {"method": "whisper"}
        result = apply_config_to_args(minimal_args, config)
        assert result.method == "whisper"

    def test_interval_overridden(self, minimal_args):
        config = {"interval": 60}
        result = apply_config_to_args(minimal_args, config)
        assert result.interval == 60

    def test_zero_interval_skipped(self, minimal_args):
        """A value of 0 for numeric fields means 'use default', so it must be ignored."""
        config = {"interval": 0}
        result = apply_config_to_args(minimal_args, config)
        assert result.interval == 30  # original default

    def test_zero_overlap_skipped(self, minimal_args):
        config = {"overlap": 0}
        result = apply_config_to_args(minimal_args, config)
        assert result.overlap == 2

    def test_zero_lm_window_seconds_skipped(self, minimal_args):
        config = {"lm_window_seconds": 0}
        result = apply_config_to_args(minimal_args, config)
        assert result.lm_window_seconds == 120

    def test_empty_string_method_skipped(self, minimal_args):
        """Empty string for non-exempt fields must be ignored."""
        config = {"method": ""}
        result = apply_config_to_args(minimal_args, config)
        assert result.method == "auto"

    def test_empty_context_is_applied(self, minimal_args):
        """'context' is exempt — empty string is a valid value and should be applied."""
        minimal_args.context = "previous context"
        config = {"context": ""}
        result = apply_config_to_args(minimal_args, config)
        assert result.context == ""

    def test_save_transcript_none_applied(self, minimal_args):
        """save_transcript=None is an explicit 'off' value and should be applied."""
        minimal_args.save_transcript = "old.txt"
        config = {"save_transcript": None}
        result = apply_config_to_args(minimal_args, config)
        assert result.save_transcript is None

    def test_debug_bool_coercion(self, minimal_args):
        config = {"debug": True}
        result = apply_config_to_args(minimal_args, config)
        assert result.debug is True

    def test_use_faster_whisper_false(self, minimal_args):
        config = {"use_faster_whisper": False}
        result = apply_config_to_args(minimal_args, config)
        assert result.use_faster_whisper is False

    def test_none_config_returns_args_unchanged(self, minimal_args):
        result = apply_config_to_args(minimal_args, None)
        assert result.method == "auto"
        assert result.interval == 30

    def test_multiple_keys_applied(self, minimal_args):
        config = {
            "method": "subtitles",
            "interval": 45,
            "language": "ru",
            "whisper_model": "small",
        }
        result = apply_config_to_args(minimal_args, config)
        assert result.method == "subtitles"
        assert result.interval == 45
        assert result.language == "ru"
        assert result.whisper_model == "small"

    def test_unknown_config_keys_ignored(self, minimal_args):
        """Extra keys in config that don't map to args should not raise."""
        config = {"totally_unknown_key": "value", "method": "whisper"}
        result = apply_config_to_args(minimal_args, config)
        assert result.method == "whisper"


class TestLoadConfig:
    def test_returns_none_for_missing_file(self, tmp_path):
        path = str(tmp_path / "missing.json")
        assert load_config(path) is None

    def test_loads_valid_json(self, tmp_path):
        data = {"method": "whisper", "interval": 60}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = load_config(str(path))
        assert result == data

    def test_returns_none_on_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert load_config(str(path)) is None


class TestCreateDefaultConfig:
    def test_creates_file_with_expected_keys(self, tmp_path):
        path = str(tmp_path / "config.json")
        cfg = create_default_config(path)
        # File was written
        assert (tmp_path / "config.json").exists()
        # Returned dict has required keys
        for key in ("method", "interval", "language", "whisper_model", "debug"):
            assert key in cfg

    def test_default_method_is_auto(self, tmp_path):
        cfg = create_default_config(str(tmp_path / "config.json"))
        assert cfg["method"] == "auto"

    def test_file_is_valid_json(self, tmp_path):
        path = tmp_path / "config.json"
        create_default_config(str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
