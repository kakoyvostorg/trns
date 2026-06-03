"""
Shared fixtures for the TRNS test suite.
"""

import json

import pytest


@pytest.fixture
def config_file(tmp_path):
    """Write a minimal config.json to a temp dir and return its path."""
    data = {
        "allowed_user_ids": [111, 222],
        "context": "",
        "daily_capacity": 1000,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest.fixture
def metadata_file(tmp_path):
    """Write a minimal metadata.json and return its path."""
    data = {
        "default_language": "ru",
        "languages": {
            "ru": {
                "no_tokens_available": "Нет доступных токенов",
                "some_key": "Привет",
            },
            "en": {
                "some_key": "Hello",
            },
        },
    }
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def api_key_file(tmp_path):
    """Write a two-token api_key.txt and return its path."""
    path = tmp_path / "api_key.txt"
    path.write_text("token-alpha\ntoken-beta\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def minimal_args():
    """Return a simple namespace that mimics parsed CLI args."""
    import argparse
    return argparse.Namespace(
        url=None,
        method="auto",
        interval=30,
        language="en",
        whisper_model="tiny",
        use_faster_whisper=True,
        translation_output="russian-only",
        save_transcript=None,
        overlap=2,
        process_mode="auto",
        lm_window_seconds=120,
        lm_interval=30,
        lm_output_mode="both",
        lm_api_key_file="api_key.txt",
        lm_prompt_file="prompt.md",
        lm_prompt_original_file="prompt_original.md",
        lm_model="google/gemma-3-27b-it:free",
        debug=False,
        context="",
    )
