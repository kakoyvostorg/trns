"""
Tests for trns.bot.utils — authentication, token management, user settings,
daily capacity, and metadata helpers.

All filesystem operations use pytest's tmp_path to stay isolated.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trns.bot.utils import (
    DAILY_CAPACITY,
    WARNING_THRESHOLD,
    add_authenticated_user,
    add_tokens,
    check_capacity_at_start,
    check_token_warning,
    decrement_daily_capacity,
    get_current_token,
    get_daily_capacity,
    get_text,
    get_timecode_default,
    get_token_count,
    get_user_setting,
    initialize_user_settings,
    is_user_authenticated,
    load_auth_key,
    load_tokens,
    load_user_settings,
    save_tokens,
    save_user_settings,
    set_user_setting,
)

# ---------------------------------------------------------------------------
# get_text
# ---------------------------------------------------------------------------

class TestGetText:
    def test_returns_text_for_default_language(self, metadata_file):
        meta = json.loads(open(metadata_file, encoding="utf-8").read())
        assert get_text(meta, "some_key") == "Привет"

    def test_explicit_language(self, metadata_file):
        meta = json.loads(open(metadata_file, encoding="utf-8").read())
        assert get_text(meta, "some_key", language="en") == "Hello"

    def test_missing_key_returns_key_itself(self, metadata_file):
        meta = json.loads(open(metadata_file, encoding="utf-8").read())
        assert get_text(meta, "nonexistent_key") == "nonexistent_key"


# ---------------------------------------------------------------------------
# load_auth_key
# ---------------------------------------------------------------------------

class TestLoadAuthKey:
    def test_env_var_takes_priority(self, tmp_path):
        key_file = tmp_path / "key.txt"
        key_file.write_text("file-key", encoding="utf-8")
        with patch.dict("os.environ", {"AUTH_KEY": "env-key"}):
            assert load_auth_key(str(key_file)) == "env-key"

    def test_falls_back_to_file(self, tmp_path):
        key_file = tmp_path / "key.txt"
        key_file.write_text("file-key\n", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            # Remove AUTH_KEY from env if present
            import os
            os.environ.pop("AUTH_KEY", None)
            assert load_auth_key(str(key_file)) == "file-key"

    def test_raises_when_file_missing_and_no_env(self, tmp_path):
        import os
        os.environ.pop("AUTH_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(FileNotFoundError):
                load_auth_key(str(tmp_path / "missing.txt"))


# ---------------------------------------------------------------------------
# is_user_authenticated
# ---------------------------------------------------------------------------

class TestIsUserAuthenticated:
    def test_known_user_returns_true(self, config_file):
        assert is_user_authenticated(111, config_path=config_file) is True

    def test_unknown_user_returns_false(self, config_file):
        assert is_user_authenticated(999, config_path=config_file) is False

    def test_missing_config_returns_false(self, tmp_path):
        assert is_user_authenticated(111, config_path=str(tmp_path / "no.json")) is False

    def test_per_user_state_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRNS_STATE_DIR", str(tmp_path / "state"))
        add_authenticated_user(444)
        assert is_user_authenticated(444, config_path=str(tmp_path / "missing.json")) is True


# ---------------------------------------------------------------------------
# add_authenticated_user
# ---------------------------------------------------------------------------

class TestAddAuthenticatedUser:
    def test_adds_new_user(self, config_file):
        add_authenticated_user(333, config_path=config_file)
        cfg = json.loads(open(config_file, encoding="utf-8").read())
        assert 333 in cfg["allowed_user_ids"]

    def test_idempotent_for_existing_user(self, config_file):
        add_authenticated_user(111, config_path=config_file)
        add_authenticated_user(111, config_path=config_file)
        cfg = json.loads(open(config_file, encoding="utf-8").read())
        assert cfg["allowed_user_ids"].count(111) == 1

    def test_does_not_remove_existing_users(self, config_file):
        add_authenticated_user(333, config_path=config_file)
        cfg = json.loads(open(config_file, encoding="utf-8").read())
        assert 111 in cfg["allowed_user_ids"]
        assert 222 in cfg["allowed_user_ids"]

    def test_default_writes_per_user_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRNS_STATE_DIR", str(tmp_path / "state"))
        add_authenticated_user(333)

        user_file = tmp_path / "state" / "users" / "333.json"
        data = json.loads(user_file.read_text(encoding="utf-8"))
        assert data["authenticated"] is True
        assert data["settings"] == {}


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

class TestTokenManagement:
    def test_load_tokens_from_file(self, api_key_file):
        import os
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            tokens = load_tokens(api_key_file)
        assert tokens == ["token-alpha", "token-beta"]

    def test_load_tokens_env_var_takes_priority(self, api_key_file):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-token"}):
            tokens = load_tokens(api_key_file)
        assert tokens == ["env-token"]

    def test_load_tokens_empty_when_file_missing(self, tmp_path):
        import os
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            tokens = load_tokens(str(tmp_path / "missing.txt"))
        assert tokens == []

    def test_save_and_reload_tokens(self, tmp_path):
        path = str(tmp_path / "keys.txt")
        import os
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            save_tokens(["tok1", "tok2"], path)
            result = load_tokens(path)
        assert result == ["tok1", "tok2"]

    def test_add_tokens_no_duplicates(self, tmp_path):
        path = str(tmp_path / "keys.txt")
        import os
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            save_tokens(["existing"], path)
            add_tokens(["existing", "new-one"], path)
            result = load_tokens(path)
        assert result.count("existing") == 1
        assert "new-one" in result

    def test_get_current_token_returns_first(self, api_key_file):
        import os
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            token = get_current_token(api_key_file)
        assert token == "token-alpha"

    def test_get_current_token_none_when_empty(self, tmp_path):
        path = str(tmp_path / "empty.txt")
        import os
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            assert get_current_token(path) is None

    def test_get_token_count(self, api_key_file):
        import os
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch.dict("os.environ", {}, clear=True):
            assert get_token_count(api_key_file) == 2


# ---------------------------------------------------------------------------
# Daily capacity
# ---------------------------------------------------------------------------

class TestDailyCapacity:
    def _write_metadata(self, tmp_path, capacity, date_str):
        data = {
            "default_language": "ru",
            "languages": {"ru": {}},
            "daily_capacity": capacity,
            "last_capacity_date": date_str,
        }
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_returns_full_capacity_on_new_day(self, tmp_path):
        path = self._write_metadata(tmp_path, 500, "2000-01-01")
        today = datetime.now(timezone.utc).date().isoformat()
        result = get_daily_capacity(path)
        assert result == DAILY_CAPACITY
        # File was updated
        data = json.loads(open(path, encoding="utf-8").read())
        assert data["last_capacity_date"] == today

    def test_returns_existing_capacity_same_day(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, 300, today)
        assert get_daily_capacity(path) == 300

    def test_decrement_reduces_capacity(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, 10, today)
        result = decrement_daily_capacity(path)
        assert result is True
        data = json.loads(open(path, encoding="utf-8").read())
        assert data["daily_capacity"] == 9

    def test_decrement_returns_true_at_last_capacity(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, 1, today)
        result = decrement_daily_capacity(path)
        # After decrement capacity is 0, returns True (last request succeeds)
        assert result is True

    def test_decrement_returns_false_when_already_zero(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, 0, today)
        result = decrement_daily_capacity(path)
        assert result is False

    def test_decrement_resets_on_new_day(self, tmp_path):
        path = self._write_metadata(tmp_path, 0, "2000-01-01")
        result = decrement_daily_capacity(path)
        # New day -> reset to 1000, then decrement to 999 -> still True
        assert result is True

    def test_check_capacity_at_start_no_warn_when_high(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, 500, today)
        capacity, should_warn = check_capacity_at_start(path)
        assert capacity == 500
        assert should_warn is False

    def test_check_capacity_at_start_warns_when_low(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, WARNING_THRESHOLD - 1, today)
        capacity, should_warn = check_capacity_at_start(path)
        assert should_warn is True

    def test_check_token_warning_true_below_threshold(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, 10, today)
        assert check_token_warning(path) is True

    def test_check_token_warning_false_above_threshold(self, tmp_path):
        today = datetime.now(timezone.utc).date().isoformat()
        path = self._write_metadata(tmp_path, 100, today)
        assert check_token_warning(path) is False

    def test_default_capacity_uses_runtime_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRNS_STATE_DIR", str(tmp_path / "state"))
        assert get_daily_capacity() == DAILY_CAPACITY

        state_file = tmp_path / "state" / "global.json"
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["daily_capacity"] == DAILY_CAPACITY
        assert data["last_capacity_date"] == datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------

class TestUserSettings:
    def test_save_and_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "settings.json")
        data = {1: {"show_transcription": False}, 2: {"show_transcription": True}}
        save_user_settings(data, path)
        loaded = load_user_settings(path)
        assert loaded[1]["show_transcription"] is False
        assert loaded[2]["show_transcription"] is True

    def test_load_creates_empty_when_missing(self, tmp_path):
        path = str(tmp_path / "settings.json")
        result = load_user_settings(path)
        assert result == {}
        assert (tmp_path / "settings.json").exists()

    def test_get_user_setting_returns_value(self, tmp_path):
        path = str(tmp_path / "settings.json")
        save_user_settings({42: {"color": "red"}}, path)
        assert get_user_setting(42, "color", settings_path=path) == "red"

    def test_get_user_setting_returns_default(self, tmp_path):
        path = str(tmp_path / "settings.json")
        save_user_settings({}, path)
        assert get_user_setting(42, "color", default="blue", settings_path=path) == "blue"

    def test_set_user_setting_creates_and_saves(self, tmp_path):
        path = str(tmp_path / "settings.json")
        set_user_setting(7, "show_transcription", False, settings_path=path)
        assert get_user_setting(7, "show_transcription", settings_path=path) is False

    def test_set_user_setting_updates_existing(self, tmp_path):
        path = str(tmp_path / "settings.json")
        set_user_setting(7, "show_transcription", True, settings_path=path)
        set_user_setting(7, "show_transcription", False, settings_path=path)
        assert get_user_setting(7, "show_transcription", settings_path=path) is False

    def test_initialize_user_settings_default_values(self, tmp_path):
        path = str(tmp_path / "settings.json")
        initialize_user_settings(99, settings_path=path)
        assert get_user_setting(99, "show_original_translation", settings_path=path) is True
        assert get_user_setting(99, "show_transcription", settings_path=path) is True

    def test_initialize_does_not_overwrite_existing(self, tmp_path):
        path = str(tmp_path / "settings.json")
        set_user_setting(99, "show_transcription", False, settings_path=path)
        initialize_user_settings(99, settings_path=path)
        # Should NOT reset to True
        assert get_user_setting(99, "show_transcription", settings_path=path) is False

    def test_default_settings_use_per_user_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRNS_STATE_DIR", str(tmp_path / "state"))
        initialize_user_settings(99)
        set_user_setting(99, "context", "demo")

        assert get_user_setting(99, "show_original_translation") is True
        assert get_user_setting(99, "show_transcription") is True
        assert get_user_setting(99, "context") == "demo"

        user_file = tmp_path / "state" / "users" / "99.json"
        data = json.loads(user_file.read_text(encoding="utf-8"))
        assert data["settings"]["context"] == "demo"


class TestTimecodeDefault:
    def test_reads_timecode_from_config_dict(self):
        assert get_timecode_default({"timecode": True}) is True
        assert get_timecode_default({"timecode": False}) is False

    def test_missing_config_key_defaults_off(self):
        assert get_timecode_default({}) is False

    def test_missing_config_file_defaults_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "missing.json"))
        assert get_timecode_default() is False
