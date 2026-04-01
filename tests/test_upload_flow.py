"""
Regression tests for Telegram video upload flow (deduped pipeline path).

Keeps tests offline: mocks TranscriptionPipeline / _run_pipeline_with_output.
"""

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import trns.bot.routes._processing as processing_mod
from trns.bot.routes import processing_lock, user_processing_tasks

# Same keys as trns.transcription.main.create_default_config (no file I/O).
_DEFAULT_CONFIG = {
    "url": "",
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
    "debug": False,
    "context": "",
}

_TRUTHY_METADATA = {"default_language": "en", "languages": {"en": {}}}


@pytest.fixture(autouse=True)
def clean_user_processing_registry():
    """Isolate tests — user_processing_tasks is process-global."""
    user_processing_tasks.clear()
    yield
    user_processing_tasks.clear()


def _run(coro):
    asyncio.run(coro)


class TestProcessVideoFile:
    """process_video_file must delegate to _run_pipeline_with_output like URL jobs."""

    def test_calls_run_pipeline_with_file_source_whisper_and_local_path(self, tmp_path):
        video_path = tmp_path / "clip.mp4"
        video_path.write_bytes(b"fake")

        uid = 9001
        jid = "upload-job-01"
        shutdown_flag = threading.Event()
        user_processing_tasks[uid] = {
            "shutdown_flag": shutdown_flag,
            "job_id": jid,
        }

        mock_pipeline = AsyncMock()
        mock_reset = MagicMock()
        client = MagicMock()
        client.send_message = AsyncMock()
        message = MagicMock()
        message.chat.id = 424242

        async def body():
            with patch.object(processing_mod, "_run_pipeline_with_output", mock_pipeline):
                with patch.object(processing_mod, "load_config_main", return_value=_DEFAULT_CONFIG):
                    with patch.object(processing_mod, "reset_context", mock_reset):
                        with patch.object(processing_mod, "get_user_setting", return_value=True):
                            with patch.object(processing_mod, "get_context", return_value="ctx"):
                                with patch("trns.bot.server.bot_metadata", _TRUTHY_METADATA):
                                    await processing_mod.process_video_file(
                                        str(video_path), uid, client, message, job_id=jid
                                    )

        _run(body())

        mock_pipeline.assert_awaited_once()
        kw = mock_pipeline.await_args.kwargs
        assert kw["video_id"] == str(video_path)
        assert kw["source"] == "file"
        assert kw["user_id"] == uid
        assert kw["client"] is client
        assert kw["chat_id"] == 424242
        assert kw["shutdown_flag"] is shutdown_flag
        assert kw["metadata"] is _TRUTHY_METADATA

        args = kw["args"]
        assert args.method == "whisper"
        assert args.process_mode == "full"
        assert args.url == str(video_path)

        mock_reset.assert_called_once_with(uid)
        assert not video_path.exists()
        assert uid not in user_processing_tasks

    def test_missing_shutdown_flag_does_not_run_pipeline(self, tmp_path):
        video_path = tmp_path / "n.mp4"
        video_path.write_bytes(b"x")

        mock_pipeline = AsyncMock()
        client = MagicMock()
        client.send_message = AsyncMock()
        message = MagicMock()
        message.chat.id = 1

        async def body():
            with patch.object(processing_mod, "_run_pipeline_with_output", mock_pipeline):
                with patch.object(processing_mod, "load_config_main", return_value=_DEFAULT_CONFIG):
                    with patch("trns.bot.server.bot_metadata", _TRUTHY_METADATA):
                        await processing_mod.process_video_file(str(video_path), 9100, client, message)

        _run(body())

        mock_pipeline.assert_not_awaited()
        client.send_message.assert_awaited()
        sent = client.send_message.await_args.kwargs["text"]
        assert "missing shutdown flag" in sent


class TestHandleVideoMessage:
    """After download, handler must schedule process_video_file with the temp path."""

    @pytest.fixture
    def project_metadata(self):
        root = Path(__file__).resolve().parents[1]
        return json.loads((root / "metadata.json").read_text(encoding="utf-8"))

    def test_download_then_calls_process_video_file_with_expected_path(
        self, tmp_path, project_metadata, monkeypatch
    ):
        import tempfile as tempfile_mod

        monkeypatch.setattr(tempfile_mod, "gettempdir", lambda: str(tmp_path))

        from trns.bot.routes import _handlers as handlers_mod

        mock_pvf = AsyncMock()
        uid = 8002

        video = MagicMock()
        video.file_size = 1000
        video.file_id = "fid999"
        video.file_name = "test.mp4"

        message = MagicMock()
        message.from_user.id = uid
        message.chat.id = 1
        message.video = video
        message.document = None
        message.reply_text = AsyncMock()

        async def download_impl(file_name=None, **kwargs):
            Path(file_name).write_bytes(b"vid")

        message.download = AsyncMock(side_effect=download_impl)
        client = MagicMock()

        async def body():
            # Keep process_video_file patched until the background task finishes — the
            # task runs after handle_video_message returns, so the patch must stay
            # in scope for await bg.
            with patch.object(handlers_mod, "is_user_authenticated", return_value=True):
                with patch.object(handlers_mod, "check_token_warning", return_value=False):
                    with patch("trns.bot.server.bot_metadata", project_metadata):
                        with patch.object(handlers_mod, "process_video_file", mock_pvf):
                            await handlers_mod.handle_video_message(client, message)
                            with processing_lock:
                                bg = user_processing_tasks.get(uid, {}).get("task")
                            assert bg is not None, "background task should be registered"
                            await bg

        _run(body())

        mock_pvf.assert_awaited_once()
        assert mock_pvf.await_args.args[0].endswith(f"telegram_video_{uid}_fid999.mp4")
        assert mock_pvf.await_args.args[1] == uid
        assert mock_pvf.await_args.kwargs.get("job_id")
