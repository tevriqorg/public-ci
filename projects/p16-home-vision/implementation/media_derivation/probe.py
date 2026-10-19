from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceProbe:
    video_codec: str
    has_audio: bool
    duration_seconds: float | None
    width: int | None
    height: int | None


def probe_source(path: Path) -> SourceProbe:
    if not path.is_file():
        raise FileNotFoundError(path)

    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed ({completed.returncode}): {completed.stderr[-2000:]}"
        )

    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("source contains no video stream")

    duration_raw = (payload.get("format") or {}).get("duration")
    duration = float(duration_raw) if duration_raw not in (None, "N/A") else None

    return SourceProbe(
        video_codec=str(video.get("codec_name") or "unknown"),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
        duration_seconds=duration,
        width=int(video["width"]) if video.get("width") is not None else None,
        height=int(video["height"]) if video.get("height") is not None else None,
    )
