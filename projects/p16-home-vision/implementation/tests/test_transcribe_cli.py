import json
import hashlib
import struct
import sys
import wave
from pathlib import Path

from media_derivation.transcribe_cli import main


def _prepared_asset(root: Path) -> Path:
    asset = root / "asset-1"
    audio = asset / "audio" / "speech.wav"
    audio.parent.mkdir(parents=True)
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(struct.pack("<4800h", *([2000] * 4800)))
    (asset / "status.json").write_text(json.dumps({
        "asset_id": "asset-1", "state": "derived",
        "stages": {"web": "done", "frames": "done", "audio": "done", "transcript": "failed"},
        "transcript": {"state": "failed", "attempts": 1, "retryable": True},
    }))
    return asset


def test_transcription_retry_does_not_rederive_media_and_recovers(tmp_path, monkeypatch, capsys):
    asset = _prepared_asset(tmp_path)
    command = tmp_path / "asr.py"
    command.write_text("print('[]')\n")
    monkeypatch.setattr(sys, "argv", [
        "p16-media-transcribe", "--asset-root", str(tmp_path), "--asset-id", "asset-1",
        "--local-asr-command", sys.executable, str(command),
    ])

    assert main() == 0
    status = json.loads((asset / "status.json").read_text())
    assert status["state"] == "derived"
    assert status["stages"]["transcript"] == "done"
    assert status["transcript"]["attempts"] == 2
    assert (asset / "transcript" / "transcript.json").is_file()
    assert "status.json" in capsys.readouterr().out


def test_transcription_retry_records_retryable_failure_without_failing_media(tmp_path, monkeypatch):
    asset = _prepared_asset(tmp_path)
    audio = asset / "audio" / "speech.wav"
    media_hash_before = hashlib.sha256(audio.read_bytes()).hexdigest()
    monkeypatch.setattr(sys, "argv", [
        "p16-media-transcribe", "--asset-root", str(tmp_path), "--asset-id", "asset-1",
        "--local-asr-command", str(tmp_path / "missing-asr"),
    ])

    assert main() == 1
    status = json.loads((asset / "status.json").read_text())
    assert status["state"] == "derived"
    assert status["stages"]["web"] == "done"
    assert status["stages"]["transcript"] == "failed"
    assert status["transcript"]["retryable"] is True
    assert status["transcript"]["attempts"] == 2
    assert status["state"] == "derived"
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == media_hash_before


def test_invalid_asr_json_is_retryable_without_changing_derived_audio(tmp_path, monkeypatch):
    asset = _prepared_asset(tmp_path)
    audio = asset / "audio" / "speech.wav"
    media_hash_before = hashlib.sha256(audio.read_bytes()).hexdigest()
    command = tmp_path / "invalid-asr.py"
    command.write_text("print('not-json')\n")
    monkeypatch.setattr(sys, "argv", [
        "p16-media-transcribe", "--asset-root", str(tmp_path), "--asset-id", "asset-1",
        "--local-asr-command", sys.executable, str(command),
    ])

    assert main() == 1
    status = json.loads((asset / "status.json").read_text())
    assert status["state"] == "derived"
    assert status["stages"] == {"web": "done", "frames": "done", "audio": "done", "transcript": "failed"}
    assert status["transcript"]["retryable"] is True
    assert status["transcript"]["attempts"] == 2
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == media_hash_before
