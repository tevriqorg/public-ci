from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class FrameManifestError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WorkingFrame:
    artifact_id: str
    asset_id: str
    file_name: str
    source_pts_seconds: float
    source_offset_ms: int
    absolute_epoch_seconds: float | None = None


@dataclass(frozen=True)
class SelectedFrame:
    frame: WorkingFrame
    selection_reasons: tuple[str, ...]
    scores: tuple[tuple[str, float], ...] = ()
    reducer_id: str = ""
    reducer_version: str = ""


@runtime_checkable
class FrameReducer(Protocol):
    def reduce(self, frames: tuple[WorkingFrame, ...]) -> tuple[SelectedFrame, ...]:
        ...


def load_working_frame_manifest(path: Path, *, asset_id: str) -> tuple[WorkingFrame, ...]:
    """Load P16's PTS-backed working-frame manifest.

    Time is always taken from source_pts_seconds. The manifest index is retained only
    as part of a deterministic artifact identifier and is never used to infer time.
    """

    if not asset_id:
        raise FrameManifestError("FRAME_MANIFEST_ASSET_ID_REQUIRED")

    frames: list[WorkingFrame] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FrameManifestError("FRAME_MANIFEST_READ_FAILED") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FrameManifestError("FRAME_MANIFEST_JSON_INVALID") from exc
        if not isinstance(row, dict):
            raise FrameManifestError("FRAME_MANIFEST_ROW_INVALID")

        file_name = row.get("file")
        pts = row.get("source_pts_seconds")
        index = row.get("index", line_number)
        absolute = row.get("absolute_epoch_seconds")

        if not isinstance(file_name, str) or not file_name:
            raise FrameManifestError("FRAME_MANIFEST_FILE_INVALID")
        if isinstance(pts, bool) or not isinstance(pts, (int, float)):
            raise FrameManifestError("FRAME_MANIFEST_PTS_REQUIRED")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise FrameManifestError("FRAME_MANIFEST_INDEX_INVALID")
        if absolute is not None and (
            isinstance(absolute, bool) or not isinstance(absolute, (int, float))
        ):
            raise FrameManifestError("FRAME_MANIFEST_ABSOLUTE_TIME_INVALID")

        pts_value = float(pts)
        if pts_value < 0:
            raise FrameManifestError("FRAME_MANIFEST_PTS_INVALID")

        frames.append(
            WorkingFrame(
                artifact_id=f"{asset_id}:frame:{index}",
                asset_id=asset_id,
                file_name=file_name,
                source_pts_seconds=pts_value,
                source_offset_ms=round(pts_value * 1000),
                absolute_epoch_seconds=float(absolute) if absolute is not None else None,
            )
        )

    if not frames:
        raise FrameManifestError("FRAME_MANIFEST_EMPTY")

    previous = -1.0
    for frame in frames:
        if frame.source_pts_seconds < previous:
            raise FrameManifestError("FRAME_MANIFEST_PTS_NOT_MONOTONIC")
        previous = frame.source_pts_seconds

    return tuple(frames)
