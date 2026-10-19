from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MediaProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaProbeResult:
    duration_ms: int
    has_video: bool
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None


class FfprobeMediaProbe:
    """Production-compatible ffprobe adapter; tests inject a fake probe."""

    def __init__(self, executable: str = "ffprobe") -> None:
        self.executable = executable

    def probe(self, path: Path) -> MediaProbeResult:
        try:
            completed = subprocess.run(
                [
                    self.executable,
                    "-v", "error",
                    "-show_format",
                    "-show_streams",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MediaProbeError("FFPROBE_EXEC_FAILED") from exc
        if completed.returncode != 0:
            raise MediaProbeError("FFPROBE_PARSE_FAILED")
        try:
            payload = json.loads(completed.stdout)
            streams = payload.get("streams") or []
            video = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
            duration_raw = (payload.get("format") or {}).get("duration")
            if duration_raw is None and video is not None:
                duration_raw = video.get("duration")
            duration_ms = round(float(duration_raw) * 1000) if duration_raw is not None else 0
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MediaProbeError("FFPROBE_OUTPUT_INVALID") from exc
        return MediaProbeResult(
            duration_ms=duration_ms,
            has_video=video is not None,
            container=(payload.get("format") or {}).get("format_name"),
            video_codec=video.get("codec_name") if video else None,
            audio_codec=audio.get("codec_name") if audio else None,
            width=video.get("width") if video else None,
            height=video.get("height") if video else None,
        )
