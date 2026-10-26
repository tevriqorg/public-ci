from __future__ import annotations

import math
import subprocess
from pathlib import Path

from app.domain.frame_reduce import SelectedFrame, WorkingFrame


class FfmpegLumaDifferenceReducer:
    """Configurable candidate reducer over P16's PTS-backed working frames.

    The threshold is mandatory: P16 has not selected a production threshold.
    This reducer emits references to the original WorkingFrame values and never
    infers timestamps from frame indexes or the nominal video frame rate.
    """

    reducer_id = "ffmpeg_luma_difference"
    reducer_version = "1"

    def __init__(
        self,
        working_root: Path,
        *,
        change_threshold: float,
        width: int = 32,
        height: int = 18,
        timeout_seconds: float = 15,
    ) -> None:
        if (isinstance(change_threshold, bool)
                or not isinstance(change_threshold, (int, float))
                or not math.isfinite(change_threshold)
                or not 0 < change_threshold <= 1):
            raise ValueError("FRAME_REDUCE_THRESHOLD_INVALID")
        if (isinstance(width, bool) or not isinstance(width, int) or width < 1
                or isinstance(height, bool) or not isinstance(height, int) or height < 1
                or isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("FRAME_REDUCE_CONFIG_INVALID")
        self.working_root = Path(working_root).resolve()
        self.change_threshold = change_threshold
        self.width = width
        self.height = height
        self.timeout_seconds = timeout_seconds

    def _pixels(self, frame: WorkingFrame) -> bytes:
        relative = Path(frame.file_name)
        if relative.is_absolute():
            raise ValueError("FRAME_REDUCE_PATH_UNSAFE")
        candidate = self.working_root / relative
        if candidate.is_symlink():
            raise ValueError("FRAME_REDUCE_PATH_UNSAFE")
        try:
            source = candidate.resolve(strict=True)
            source.relative_to(self.working_root)
        except (OSError, ValueError):
            raise ValueError("FRAME_REDUCE_PATH_UNSAFE") from None
        if not source.is_file():
            raise ValueError("FRAME_REDUCE_INPUT_NOT_FILE")

        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(source), "-vf",
            f"scale={self.width}:{self.height}:flags=area,format=gray",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("FRAME_REDUCE_DECODE_FAILED") from None
        if completed.returncode != 0 or len(completed.stdout) != self.width * self.height:
            raise ValueError("FRAME_REDUCE_DECODE_FAILED")
        return completed.stdout

    def reduce(self, frames: tuple[WorkingFrame, ...]) -> tuple[SelectedFrame, ...]:
        if not frames:
            return ()
        if any(left.source_pts_seconds > right.source_pts_seconds
               for left, right in zip(frames, frames[1:])):
            raise ValueError("FRAME_REDUCE_PTS_NOT_MONOTONIC")

        selected: list[SelectedFrame] = []
        previous_pixels: bytes | None = None
        for frame in frames:
            pixels = self._pixels(frame)
            change = 1.0 if previous_pixels is None else sum(
                abs(left - right) for left, right in zip(previous_pixels, pixels)
            ) / (255.0 * len(pixels))
            if previous_pixels is None or change >= self.change_threshold:
                selected.append(SelectedFrame(
                    frame=frame,
                    selection_reasons=("initial_context" if previous_pixels is None
                                       else "image_difference",),
                    scores=(("normalized_luma_change", round(change, 8)),),
                    reducer_id=self.reducer_id,
                    reducer_version=self.reducer_version,
                ))
            previous_pixels = pixels
        return tuple(selected)
