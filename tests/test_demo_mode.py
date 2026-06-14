"""Tests for Telegram bot demo authentication and per-user video quota."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import trns.bot.routes._handlers as handlers_mod
from trns.bot.routes import (
    processing_lock,
    user_processing_tasks,
)
from trns.bot.routes._state import STATE_WAITING_KEY, get_user_state, set_user_state
from trns.bot.utils import add_authenticated_user, add_demo_authenticated_user


_METADATA = {
    "default_language": "en",
    "languages": {
        "en": {
            "start_message": "Welcome",
            "invalid_key": "Invalid key",
            "auth_success": "Authenticated",
            "demo_auth_success": "Demo mode enabled. You have {limit} videos available.",
            "demo_limit_reached": "Demo limit reached. This CV demo allows {limit} videos per Telegram user.",
            "context_button": "Context",
            "cancel_button": "Cancel",
            "show_original_translation_button": "Show original",
            "hide_original_translation_button": "Hide original",
            "show_transcription_button": "Show transcription",
            "hide_transcription_button": "Hide transcription",
            "show_timecodes_button": "Show timecodes",
            "hide_timecodes_button": "Hide timecodes",
            "processing_video": "Processing video...",
            "processing_youtube": "Processing YouTube video...",
            "processing_twitter": "Processing Twitter video...",
            "downloading_video": "Downloading video...",
            "not_authenticated": "Not authenticated",
            "no_video_file": "No video",
            "unknown_text": "Unknown",
            "token_warning": "Token warning",
            "error_occurred": "Error:",
        }
    },
}


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TRNS_STATE_DIR", str(tmp_path / "state"))
    user_processing_tasks.clear()
    yield
    user_processing_tasks.clear()


def _run(coro):
    asyncio.run(coro)


def _message(uid=7001, text=None):
    message = MagicMock()
    message.from_user.id = uid
    message.chat.id = uid * 10
    message.text = text
    message.video = None
    message.document = None
    message.reply_text = AsyncMock()
    return message


class TestDemoAuthentication:
    def test_demo_key_authenticates_user_with_limit(self):
        uid = 7001
        message = _message(uid=uid, text="demo-key")
        set_user_state(uid, STATE_WAITING_KEY)

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch("trns.bot.server.create_keyboard", return_value="keyboard"):
                    with patch.object(handlers_mod, "load_auth_key", side_effect=FileNotFoundError):
                        with patch.object(handlers_mod, "load_demo_auth_key", return_value="demo-key"):
                            with patch.object(handlers_mod, "get_demo_video_limit", return_value=10):
                                await handlers_mod.handle_text_message(MagicMock(), message)

        _run(body())

        message.reply_text.assert_awaited_once_with(
            "Demo mode enabled. You have 10 videos available.",
            reply_markup="keyboard",
        )
        assert get_user_state(uid) is None

    def test_normal_key_authenticates_full_user(self):
        uid = 7002
        message = _message(uid=uid, text="full-key")
        set_user_state(uid, STATE_WAITING_KEY)

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch("trns.bot.server.create_keyboard", return_value="keyboard"):
                    with patch.object(handlers_mod, "load_auth_key", return_value="full-key"):
                        with patch.object(handlers_mod, "load_demo_auth_key", return_value="demo-key"):
                            await handlers_mod.handle_text_message(MagicMock(), message)

        _run(body())

        message.reply_text.assert_awaited_once_with(
            "Authenticated",
            reply_markup="keyboard",
        )
        assert get_user_state(uid) is None


class TestDemoQuotaHandlers:
    def test_youtube_demo_user_is_accepted_when_quota_remains(self):
        uid = 7101
        add_demo_authenticated_user(uid, video_limit=1)
        message = _message(uid=uid, text="https://www.youtube.com/watch?v=abcdefghijk")
        mock_process = AsyncMock()

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch.object(handlers_mod, "check_capacity_at_start", return_value=(1000, False)):
                    with patch.object(handlers_mod, "process_youtube_video", mock_process):
                        await handlers_mod.handle_text_message(MagicMock(), message)
                        with processing_lock:
                            task = user_processing_tasks.get(uid, {}).get("task")
                        assert task is not None
                        await task

        _run(body())

        mock_process.assert_awaited_once()
        assert message.reply_text.await_args_list[0].args[0] == "Processing YouTube video..."

    def test_youtube_demo_user_is_rejected_after_quota_exhausted(self):
        uid = 7102
        add_demo_authenticated_user(uid, video_limit=0)
        message = _message(uid=uid, text="https://www.youtube.com/watch?v=abcdefghijk")
        mock_process = AsyncMock()

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch.object(handlers_mod, "process_youtube_video", mock_process):
                    await handlers_mod.handle_text_message(MagicMock(), message)

        _run(body())

        mock_process.assert_not_awaited()
        message.reply_text.assert_awaited_once_with(
            "Demo limit reached. This CV demo allows 0 videos per Telegram user."
        )
        assert uid not in user_processing_tasks

    def test_upload_demo_user_is_rejected_after_quota_exhausted(self):
        uid = 7103
        add_demo_authenticated_user(uid, video_limit=0)
        message = _message(uid=uid)
        video = MagicMock()
        video.file_size = 100
        video.file_id = "file-1"
        video.file_name = "clip.mp4"
        message.video = video
        message.download = AsyncMock()

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch("trns.bot.server.create_keyboard", return_value="keyboard"):
                    with patch.object(handlers_mod, "check_token_warning", return_value=False):
                        await handlers_mod.handle_video_message(MagicMock(), message)

        _run(body())

        message.download.assert_not_awaited()
        message.reply_text.assert_awaited_once_with(
            "Demo limit reached. This CV demo allows 0 videos per Telegram user."
        )
        assert uid not in user_processing_tasks

    def test_full_user_bypasses_demo_quota(self):
        uid = 7104
        add_authenticated_user(uid)
        message = _message(uid=uid, text="https://www.youtube.com/watch?v=abcdefghijk")
        mock_process = AsyncMock()

        async def body():
            with patch("trns.bot.server.bot_metadata", _METADATA):
                with patch.object(handlers_mod, "check_capacity_at_start", return_value=(1000, False)):
                    with patch.object(handlers_mod, "process_youtube_video", mock_process):
                        await handlers_mod.handle_text_message(MagicMock(), message)
                        with processing_lock:
                            task = user_processing_tasks.get(uid, {}).get("task")
                        assert task is not None
                        await task

        _run(body())

        mock_process.assert_awaited_once()
