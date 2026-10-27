from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from media_derivation.probe import probe_source

_SHOWINFO_RE = re.compile(r"\bn:\s*(?P<n>\d+)\b.*?\bpts_time:(?P<pts>-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class DerivationPaths:
    raw: Path
    root: Path

    @property
    def web_dir(self) -> Path:
        return self.root / "web"

    @property
    def frames_work_dir(self) -> Path:
        return self.root / "frames.work"

    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def web_part(self) -> Path:
        return self.web_dir / "video.mp4.part"

    @property
    def web_final(self) -> Path:
        return self.web_dir / "video.mp4"

    @property
    def audio_part(self) -> Path:
        return self.audio_dir / "speech.wav.part"

    @property
    def audio_final(self) -> Path:
        return self.audio_dir / "speech.wav"

    @property
    def frame_manifest_work(self) -> Path:
        return self.frames_work_dir / "manifest.jsonl"

    @property
    def frame_manifest_final(self) -> Path:
        return self.frames_dir / "manifest.jsonl"


@dataclass(frozen=True)
class DerivationPlan:
    has_audio: bool
    sample_fps: float = 1.0
    jpeg_quality: int = 3
    audio_rate_hz: int = 16_000


def build_ffmpeg_command(
    paths: DerivationPaths, plan: DerivationPlan, *, video_codec: str
) -> list[str]:
    """Build one-input/multi-output FFmpeg command.

    Web playback keeps the video bitstream by stream copy. Only HEVC needs an
    explicit browser-compatible hvc1 tag; other codecs use the MP4 muxer's tag.
    A decoded video branch emits 1 fps working frames, while audio is normalized
    to mono 16 kHz WAV for local ASR when an audio stream exists.
    """

    if (isinstance(plan.sample_fps, bool)
            or not isinstance(plan.sample_fps, (int, float))
            or not math.isfinite(plan.sample_fps)
            or plan.sample_fps <= 0):
        raise ValueError("SAMPLE_FPS_INVALID")
    sample_interval_seconds = 1.0 / plan.sample_fps
    frame_filter = (
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{sample_interval_seconds})',showinfo"
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        # Keep each filter graph within the worker's explicit low-memory
        # resource envelope. FFmpeg otherwise sizes filter pools from the host
        # CPU count, which can fail on constrained workers while producing the
        # parallel 1fps JPEG branch.
        "-filter_threads",
        "1",
        "-i",
        str(paths.raw),
        # Web media branch: packet copy for video, AAC only for browser audio.
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(paths.web_part),
        # Select on source time and preserve each selected input frame's PTS.
        "-map",
        "0:v:0",
        "-vf",
        frame_filter,
        "-fps_mode",
        "vfr",
        "-q:v",
        str(plan.jpeg_quality),
        str(paths.frames_work_dir / "%08d.jpg"),
    ]

    if video_codec.lower() in {"hevc", "h265"}:
        insert_at = cmd.index("-c:a")
        cmd[insert_at:insert_at] = ["-tag:v", "hvc1"]

    if plan.has_audio:
        cmd += [
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(plan.audio_rate_hz),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(paths.audio_part),
        ]

    return cmd


def parse_showinfo_manifest(
    stderr_text: str,
    frame_names: Iterable[str],
    *,
    source_start_epoch: float | None = None,
) -> list[dict[str, object]]:
    """Pair emitted frame names with real media PTS.

    Never infer time from frame index or nominal FPS. This matters for the real P16
    inventory where most source recordings are VFR/variable cadence.
    """

    pts_values: list[float] = []
    for line in stderr_text.splitlines():
        match = _SHOWINFO_RE.search(line)
        if match:
            pts_values.append(float(match.group("pts")))

    names = list(frame_names)
    if len(pts_values) != len(names):
        raise ValueError(
            f"showinfo/frame mismatch: pts={len(pts_values)} files={len(names)}"
        )

    result: list[dict[str, object]] = []
    for index, (name, pts) in enumerate(zip(names, pts_values), start=1):
        row: dict[str, object] = {
            "index": index,
            "file": name,
            "source_pts_seconds": pts,
        }
        if source_start_epoch is not None:
            row["absolute_epoch_seconds"] = source_start_epoch + pts
        result.append(row)
    return result


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def _prepare(paths: DerivationPaths) -> None:
    paths.web_dir.mkdir(parents=True, exist_ok=True)
    paths.audio_dir.mkdir(parents=True, exist_ok=True)
    if paths.frames_work_dir.exists():
        shutil.rmtree(paths.frames_work_dir)
    paths.frames_work_dir.mkdir(parents=True, exist_ok=True)

    for part in (paths.web_part, paths.audio_part):
        part.unlink(missing_ok=True)


def run_derivation(
    paths: DerivationPaths,
    plan: DerivationPlan,
    *,
    source_start_epoch: float | None = None,
    video_codec: str | None = None,
) -> dict[str, object]:
    """Run the transactional media-derivation PoC.

    RAW is read-only input. No deletion path exists here. Outputs are promoted only
    after FFmpeg succeeds and PTS manifest cardinality matches the emitted frames.
    """

    if not paths.raw.is_file():
        raise FileNotFoundError(paths.raw)

    _prepare(paths)
    codec = video_codec or probe_source(paths.raw).video_codec
    completed = subprocess.run(
        build_ffmpeg_command(paths, plan, video_codec=codec),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({completed.returncode}): {completed.stderr[-4000:]}")

    frame_files = sorted(p.name for p in paths.frames_work_dir.glob("*.jpg"))
    if not frame_files:
        raise RuntimeError("ffmpeg succeeded but emitted no 1fps frames")

    manifest = parse_showinfo_manifest(
        completed.stderr,
        frame_files,
        source_start_epoch=source_start_epoch,
    )
    _write_manifest(paths.frame_manifest_work, manifest)

    if not paths.web_part.is_file() or paths.web_part.stat().st_size == 0:
        raise RuntimeError("web media output missing or empty")
    if plan.has_audio and (
        not paths.audio_part.is_file() or paths.audio_part.stat().st_size == 0
    ):
        raise RuntimeError("audio output missing or empty")

    os.replace(paths.web_part, paths.web_final)
    if plan.has_audio:
        os.replace(paths.audio_part, paths.audio_final)

    if paths.frames_dir.exists():
        shutil.rmtree(paths.frames_dir)
    os.replace(paths.frames_work_dir, paths.frames_dir)

    return {
        "web_media": str(paths.web_final),
        "frames_dir": str(paths.frames_dir),
        "frame_count": len(manifest),
        "frame_manifest": str(paths.frame_manifest_final),
        "audio": str(paths.audio_final) if plan.has_audio else None,
        "raw_deleted": False,
    }
