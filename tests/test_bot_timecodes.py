"""
Tests for Telegram bot timecode settings and pipeline wiring.

All cases stay offline by mocking Telegram sends and the transcription pipeline.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import trns.bot.server as server_mod
import trns.bot.routes._handlers as handlers_mod
import trns.bot.routes._processing as processing_mod
from trns.bot.routes import user_processing_tasks


_METADATA = {
    "default_language": "en",
    "languages": {
        "en": {
            "context_button": "Context",
            "cancel_button": "Cancel",
            "show_original_translation_button": "Show original",
            "hide_original_translation_button": "Hide original",
            "show_transcription_button": "Show transcription",
            "hide_transcription_button": "Hide transcription",
            "show_timecodes_button": "Show timecodes",
            "hide_timecodes_button": "Hide timecodes",
            "timecodes_enabled": "Timecodes enabled.",
            "timecodes_disabled": "Timecodes disabled.",
        }
    },
}


_CONFIG = {
    "method": "auto",
    "interval": 30,
    "language": "en",
    "whisper_model": "auto",
    "use_faster_whisper": True,
    "translation_output": "russian-only",
    "save_transcript": None,
    "overlap": 2,
    "process_mode": "auto",
    "lm_window_seconds": 120,
    "lm_interval": 30,
    "lm_output_mode": "both",
    "lm_api_key_file": "api_key.txt",
    "lm_prompt_file": "prompt.md",
    "lm_prompt_original_file": "prompt_original.md",
    "lm_model": "google/gemma-3-27b-it:free",
    "timecode": True,
    "timecode_in_summary": False,
    "timecode_min_interval_seconds": 7.0,
    "yt_dlp_cookie_file": None,
    "yt_dlp_cookies_from_browser": None,
    "debug": False,
    "context": "",
}


def _run(coro):
    asyncio.run(coro)


def _button_rows(markup):
    return [[button.text for button in row] for row in markup.keyboard]


def _fake_setting(values):
    def get_setting(_user_id, key, default=None):
        return values.get(key, default)

    return get_setting


class TestTimecodeKeyboard:
    def test_keyboard_shows_timecode_enable_button_by_default(self):
        with patch.object(server_mod, "get_timecode_default", return_value=False):
            with patch.object(server_mod, "get_user_setting", side_effect=_fake_setting({})):
                keyboard = server_mod.create_keyboard(_METADATA, user_id=42)

        rows = _button_rows(keyboard)
        assert rows[0] == ["Hide original", "Hide transcription"]
        assert rows[1] == ["Show timecodes"]
        assert rows[2] == ["Context"]
        assert rows[3] == ["Cancel"]

    def test_keyboard_shows_timecode_disable_button_when_enabled(self):
        values = {"timecode_enabled": True}
        with patch.object(server_mod, "get_timecode_default", return_value=False):
            with patch.object(server_mod, "get_user_setting", side_effect=_fake_setting(values)):
                keyboard = server_mod.create_keyboard(_METADATA, user_id=42)

        assert _button_rows(keyboard)[1] == ["Hide timecodes"]


class TestTimecodeToggle:
    def test_timecode_button_dispatches_to_toggle_handler(self):
        client = MagicMock()
        message = MagicMock()
        message.from_user.id = 42
        message.text = "Show timecodes"

        mock_toggle = AsyncMock()

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch.object(handlers_mod, "is_user_authenticated", return_value=True):
                    with patch.object(handlers_mod, "handle_toggle_timecodes_button", mock_toggle):
                        await handlers_mod.handle_text_message(client, message)

        _run(body())

        mock_toggle.assert_awaited_once_with(client, message)

    def test_toggle_persists_new_timecode_setting(self):
        client = MagicMock()
        message = MagicMock()
        message.from_user.id = 42
        message.reply_text = AsyncMock()

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch("trns.bot.server.create_keyboard", return_value="keyboard"):
                    with patch.object(handlers_mod, "get_user_setting", return_value=False):
                        with patch.object(handlers_mod, "set_user_setting") as mock_set:
                            await handlers_mod.handle_toggle_timecodes_button(client, message)
                            mock_set.assert_called_once_with(42, "timecode_enabled", True)

        _run(body())

        message.reply_text.assert_awaited_once_with(
            "Timecodes enabled.",
            reply_markup="keyboard",
        )


class TestTimecodeProcessingArgs:
    def setup_method(self):
        user_processing_tasks.clear()

    def teardown_method(self):
        user_processing_tasks.clear()

    def test_url_processing_inherits_config_timecode_default(self):
        uid = 5001
        shutdown_flag = threading.Event()
        user_processing_tasks[uid] = {"shutdown_flag": shutdown_flag, "job_id": "yt-job"}

        client = MagicMock()
        message = MagicMock()
        message.chat.id = 123
        mock_pipeline = AsyncMock()

        async def body():
            with patch.object(processing_mod, "_run_pipeline_with_output", mock_pipeline):
                with patch.object(processing_mod, "load_config_main", return_value=dict(_CONFIG)):
                    with patch.object(processing_mod, "get_user_setting", side_effect=_fake_setting({})):
                        with patch.object(processing_mod, "get_context", return_value="ctx"):
                            with patch.object(processing_mod, "reset_context"):
                                await processing_mod.process_youtube_video(
                                    "https://www.youtube.com/watch?v=abcdefghijk",
                                    uid,
                                    client,
                                    message,
                                    job_id="yt-job",
                                )

        _run(body())

        args = mock_pipeline.await_args.kwargs["args"]
        assert args.timecode is True
        assert args.timecode_in_summary is True
        assert args.timecode_min_interval_seconds == 7.0

    def test_uploaded_file_processing_uses_user_timecode_override(self, tmp_path):
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"fake")

        uid = 5002
        shutdown_flag = threading.Event()
        user_processing_tasks[uid] = {"shutdown_flag": shutdown_flag, "job_id": "file-job"}

        client = MagicMock()
        client.send_message = AsyncMock()
        message = MagicMock()
        message.chat.id = 456
        mock_pipeline = AsyncMock()

        async def body():
            with patch.object(processing_mod, "_run_pipeline_with_output", mock_pipeline):
                with patch.object(processing_mod, "load_config_main", return_value=dict(_CONFIG)):
                    with patch.object(
                        processing_mod,
                        "get_user_setting",
                        side_effect=_fake_setting({"timecode_enabled": False}),
                    ):
                        with patch.object(processing_mod, "get_context", return_value="ctx"):
                            with patch.object(processing_mod, "reset_context"):
                                with patch("trns.bot.server.bot_metadata", _METADATA):
                                    await processing_mod.process_video_file(
                                        str(video_path),
                                        uid,
                                        client,
                                        message,
                                        job_id="file-job",
                                    )

        _run(body())

        args = mock_pipeline.await_args.kwargs["args"]
        assert args.timecode is False
        assert args.timecode_in_summary is False
        assert args.timecode_min_interval_seconds == 7.0
