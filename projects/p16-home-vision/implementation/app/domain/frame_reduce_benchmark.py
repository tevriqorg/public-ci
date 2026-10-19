from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Iterable

from app.domain.frame_reduce import FrameReducer, WorkingFrame


@dataclass(frozen=True)
class ReducerBenchmark:
    reducer_id: str
    reducer_version: str
    input_frames: int
    selected_frames: int
    reduction_ratio: float
    reference_recall: float
    mean_reference_error_seconds: float | None
    elapsed_ms: float


def benchmark_reducer(
    reducer: FrameReducer,
    frames: tuple[WorkingFrame, ...],
    reference_offsets_seconds: Iterable[float],
    *, tolerance_seconds: float = 2.0,
) -> ReducerBenchmark:
    if not frames or tolerance_seconds <= 0:
        raise ValueError("FRAME_BENCHMARK_INPUT_INVALID")
    start = time.perf_counter()
    selected = reducer.reduce(frames)
    elapsed = (time.perf_counter() - start) * 1000
    selected_frames = [row.frame for row in selected]
    selected_offsets = [row.source_pts_seconds for row in selected_frames]
    if len(set(frame.artifact_id for frame in selected_frames)) != len(selected_frames):
        raise ValueError("FRAME_BENCHMARK_DUPLICATE_SELECTION")
    if not set(frame.artifact_id for frame in selected_frames) <= set(frame.artifact_id for frame in frames):
        raise ValueError("FRAME_BENCHMARK_FOREIGN_FRAME")
    refs = tuple(float(value) for value in reference_offsets_seconds)
    errors = [min((abs(ref - actual) for actual in selected_offsets), default=float("inf")) for ref in refs]
    matched = [error for error in errors if error <= tolerance_seconds]
    reducer_id = next((row.reducer_id for row in selected if row.reducer_id), type(reducer).__name__)
    reducer_version = next((row.reducer_version for row in selected if row.reducer_version), "unknown")
    return ReducerBenchmark(
        reducer_id, reducer_version, len(frames), len(selected_frames),
        round(len(selected_frames) / len(frames), 6),
        round(len(matched) / len(refs), 6) if refs else 1.0,
        round(sum(matched) / len(matched), 6) if matched else None,
        round(elapsed, 3),
    )


def benchmark_report(results: Iterable[ReducerBenchmark], *, dataset_id: str, dataset_revision: str) -> dict[str, object]:
    rows = list(results)
    if not dataset_id or not dataset_revision or not rows:
        raise ValueError("FRAME_BENCHMARK_METADATA_REQUIRED")
    return {
        "schema": "p16.frame_reduce_benchmark.v1",
        "dataset": {"id": dataset_id, "revision": dataset_revision},
        "results": [asdict(row) for row in rows],
        "selection_policy": "human-reviewed reference timestamps; report recall, reduction ratio and runtime; no automatic threshold promotion",
    }


def write_benchmark_report(path, report: dict[str, object]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
