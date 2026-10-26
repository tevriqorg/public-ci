from __future__ import annotations

import argparse
import json
from pathlib import Path

from media_derivation.cli import _write_json_atomic
from media_derivation.local_speech import JsonCommandASR, build_transcript, write_transcript


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry P16 local transcription without re-deriving media")
    parser.add_argument("--asset-root", required=True, type=Path)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--local-asr-command", nargs="+", required=True)
    parser.add_argument("--asr-timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.asr_timeout_seconds <= 0:
        parser.error("ASR_TIMEOUT_MUST_BE_POSITIVE")

    root = args.asset_root.resolve()
    asset_dir = (root / args.asset_id).resolve()
    if not asset_dir.is_relative_to(root):
        parser.error("ASSET_PATH_UNSAFE")
    status_path = asset_dir / "status.json"
    if not status_path.is_file():
        parser.error("DERIVATION_STATUS_MISSING")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("asset_id") != args.asset_id or status.get("state") != "derived":
        parser.error("MEDIA_DERIVATION_NOT_READY")
    if status.get("stages", {}).get("audio") != "done":
        parser.error("DERIVED_AUDIO_NOT_AVAILABLE")
    audio_path = asset_dir / "audio" / "speech.wav"
    if not audio_path.is_file():
        parser.error("DERIVED_AUDIO_NOT_AVAILABLE")

    previous = status.get("transcript")
    attempts = previous.get("attempts", 0) if isinstance(previous, dict) else 0
    asr = JsonCommandASR(
        tuple(args.local_asr_command), backend_id=Path(args.local_asr_command[0]).name,
        timeout_seconds=args.asr_timeout_seconds,
    )
    try:
        transcript = build_transcript(args.asset_id, audio_path, asr)
        transcript_path = asset_dir / "transcript" / "transcript.json"
        write_transcript(transcript_path, transcript)
        status["transcript"] = {
            "state": "done", "attempts": attempts + 1,
            "path": str(transcript_path), "schema": transcript.schema,
            "segments": len(transcript.segments),
            "asr_pipeline_version": transcript.asr_pipeline_version,
        }
        status["stages"]["transcript"] = "done"
    except Exception as exc:
        status["transcript"] = {
            "state": "failed", "attempts": attempts + 1,
            "error_code": "LOCAL_TRANSCRIPTION_FAILED",
            "error": str(exc)[-2000:], "retryable": True,
        }
        status["stages"]["transcript"] = "failed"
        _write_json_atomic(status_path, status)
        return 1
    _write_json_atomic(status_path, status)
    print(status_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
