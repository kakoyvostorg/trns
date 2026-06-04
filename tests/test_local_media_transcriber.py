import subprocess
from pathlib import Path

from trns.transcription.whisper_transcriber import WhisperTranscriber


def _bare_transcriber():
    transcriber = object.__new__(WhisperTranscriber)
    transcriber.shutdown_flag = None
    transcriber._video_info_cache = {}
    transcriber._cache_ttl = 300
    return transcriber


def test_get_video_info_for_local_file_uses_ffprobe(monkeypatch, tmp_path):
    video_path = tmp_path / "telegram_upload.MP4"
    video_path.write_bytes(b"fake-video")

    calls = []

    def fake_run(command, check=False, capture_output=False, text=False):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="30.5\n", stderr="")

    monkeypatch.setattr("trns.transcription.whisper_transcriber.subprocess.run", fake_run)

    transcriber = _bare_transcriber()
    info = transcriber._get_video_info(str(video_path))

    assert info["is_live"] is False
    assert info["duration"] == 30.5
    assert info["title"] == "telegram_upload.MP4"
    assert calls
    assert calls[0][0] == "ffprobe"


def test_extract_audio_from_local_file_uses_ffmpeg(monkeypatch, tmp_path):
    video_path = tmp_path / "telegram_upload.MP4"
    video_path.write_bytes(b"fake-video")

    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(temp_dir))

    calls = []

    def fake_run(command, check=False, capture_output=False, text=False):
        calls.append(command)
        Path(command[-1]).write_bytes(b"fake-wav")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("trns.transcription.whisper_transcriber.subprocess.run", fake_run)

    transcriber = _bare_transcriber()
    audio_path = transcriber.extract_audio_from_youtube(str(video_path), duration=30)

    assert audio_path is not None
    assert Path(audio_path).exists()
    assert Path(audio_path).read_bytes() == b"fake-wav"
    assert calls
    assert calls[0][0] == "ffmpeg"
    assert str(video_path) in calls[0]


def test_extract_local_audio_chunk_passes_time_range(monkeypatch, tmp_path):
    video_path = tmp_path / "telegram_upload.MP4"
    video_path.write_bytes(b"fake-video")

    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(temp_dir))

    calls = []

    def fake_run(command, check=False, capture_output=False, text=False):
        calls.append(command)
        Path(command[-1]).write_bytes(b"fake-wav")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("trns.transcription.whisper_transcriber.subprocess.run", fake_run)

    transcriber = _bare_transcriber()
    audio_path = transcriber.extract_audio_from_youtube(str(video_path), duration=12, start_time=3)

    assert audio_path is not None
    command = calls[0]
    assert command[0] == "ffmpeg"
    assert command[1:3] == ["-y", "-ss"]
    assert command[3] == "3"
    assert "-t" in command
    assert command[command.index("-t") + 1] == "12"
