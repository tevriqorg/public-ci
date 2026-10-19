from dataclasses import replace

import pytest

from app.domain.frame_reduce import SelectedFrame, WorkingFrame
from app.domain.frame_reduce_benchmark import benchmark_reducer, benchmark_report


class EveryOtherFrame:
    def reduce(self, frames):
        return tuple(SelectedFrame(frame, ("fixture",), reducer_id="every_other", reducer_version="1") for frame in frames[::2])


def test_benchmark_reports_reference_recall_and_reduction():
    frames = tuple(WorkingFrame(f"f{i}", "asset", f"{i}.jpg", float(i), i * 1000) for i in range(10))
    result = benchmark_reducer(EveryOtherFrame(), frames, (0.0, 4.0, 10.0), tolerance_seconds=1.0)
    assert result.selected_frames == 5
    assert result.reduction_ratio == 0.5
    assert result.reference_recall == pytest.approx(2 / 3)
    assert benchmark_report([result], dataset_id="synthetic-fixture", dataset_revision="v1")["schema"] == "p16.frame_reduce_benchmark.v1"


class MutatingFrameReducer:
    def reduce(self, frames):
        return (SelectedFrame(replace(frames[0], source_pts_seconds=frames[0].source_pts_seconds + 99), ("mutated",)),)


def test_benchmark_rejects_selected_frame_with_forged_identity():
    frames = (WorkingFrame("f0", "asset", "0.jpg", 0.0, 0),)
    with pytest.raises(ValueError, match="FRAME_BENCHMARK_MUTATED_FRAME"):
        benchmark_reducer(MutatingFrameReducer(), frames, (0.0,))
