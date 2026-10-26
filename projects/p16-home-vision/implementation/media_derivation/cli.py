from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from media_derivation.pipeline import DerivationPaths, DerivationPlan, run_derivation
from media_derivation.probe import probe_source
from media_derivation.local_speech import JsonCommandASR, build_transcript, write_transcript


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="P16R2 media derivation PoC")
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--asset-root", required=True, type=Path)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--camera", default="unknown")
    parser.add_argument("--started-at", default=None)
    parser.add_argument("--source-start-epoch", type=float, default=None)
    parser.add_argument(
        "--local-asr-command", nargs="+", default=None,
        help="Local executable receiving one JSON request argument and returning a JSON segment array on stdout.",
    )
    args = parser.parse_args()

    asset_dir = args.asset_root / args.asset_id
    status_path = asset_dir / "status.json"
    source = probe_source(args.raw)

    base_status: dict[str, object] = {
        "schema_version": 1,
        "asset_id": args.asset_id,
        "camera": args.camera,
        "started_at": args.started_at,
        "raw_path": str(args.raw),
        "raw_deleted": False,
        "source": {
            "video_codec": source.video_codec,
            "has_audio": source.has_audio,
            "duration_seconds": source.duration_seconds,
            "width": source.width,
            "height": source.height,
        },
    }

    _write_json_atomic(
        status_path,
        {
            **base_status,
            "state": "running",
            "stages": {
                "raw": "done",
                "web": "running",
                "frames": "running",
                "audio": "running" if source.has_audio else "skipped",
                "transcript": "idle",
                "reduce": "idle",
                "package": "idle",
            },
        },
    )

    try:
        result = run_derivation(
            DerivationPaths(raw=args.raw, root=asset_dir),
            DerivationPlan(has_audio=source.has_audio),
            source_start_epoch=args.source_start_epoch,
            video_codec=source.video_codec,
        )
    except Exception as exc:
        _write_json_atomic(
            status_path,
            {
                **base_status,
                "state": "failed",
                "error_code": "MEDIA_DERIVATION_FAILED",
                "error": str(exc)[-2000:],
                "stages": {
                    "raw": "done",
                    "web": "failed",
                    "frames": "failed",
                    "audio": "failed" if source.has_audio else "skipped",
                    "transcript": "idle",
                    "reduce": "idle",
                    "package": "idle",
                },
            },
        )
        raise

    transcript_state = "not_configured"
    transcript_result = None
    if source.has_audio and args.local_asr_command:
        transcript_state = "running"
        _write_json_atomic(status_path, {
            **base_status, "state": "derived", "result": result,
            "stages": {"raw": "done", "web": "done", "frames": "done", "audio": "done", "transcript": transcript_state, "reduce": "queued", "package": "awaiting_analysis"},
        })
        try:
            asr = JsonCommandASR(
                tuple(args.local_asr_command),
                backend_id=Path(args.local_asr_command[0]).name,
            )
            transcript = build_transcript(args.asset_id, asset_dir / "audio" / "speech.wav", asr)
            transcript_path = asset_dir / "transcript" / "transcript.json"
            write_transcript(transcript_path, transcript)
            transcript_result = {"path": str(transcript_path), "schema": transcript.schema, "segments": len(transcript.segments)}
            transcript_state = "done"
        except Exception as exc:
            transcript_result = {
                "state": "failed",
                "attempts": 1,
                "error_code": "LOCAL_TRANSCRIPTION_FAILED",
                "error": str(exc)[-2000:],
                "retryable": True,
            }
            transcript_state = "failed"

    _write_json_atomic(
        status_path,
        {
            **base_status,
            "state": "derived",
            "result": result,
            "transcript": transcript_result,
            "stages": {
                "raw": "done",
                "web": "done",
                "frames": "done",
                "audio": "done" if source.has_audio else "skipped",
                "transcript": transcript_state if source.has_audio else "skipped",
                "reduce": "queued",
                "package": "awaiting_analysis",
            },
        },
    )
    print(status_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
