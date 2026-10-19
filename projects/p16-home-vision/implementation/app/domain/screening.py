from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Sequence


class ScreeningError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FrameSample:
    timestamp_ms: int
    grayscale: bytes


@dataclass(frozen=True)
class ScreeningPolicy:
    frame_threshold: float = 0.08
    min_active_ratio: float = 0.10
    sample_stride: int = 1
    merge_gap_ms: int = 2_000

    def validate(self) -> None:
        if not 0 <= self.frame_threshold <= 1:
            raise ScreeningError("SCREEN_POLICY_THRESHOLD_INVALID")
        if not 0 <= self.min_active_ratio <= 1:
            raise ScreeningError("SCREEN_POLICY_RATIO_INVALID")
        if self.sample_stride < 1:
            raise ScreeningError("SCREEN_POLICY_STRIDE_INVALID")
        if self.merge_gap_ms < 0:
            raise ScreeningError("SCREEN_POLICY_MERGE_GAP_INVALID")


@dataclass(frozen=True)
class CandidateWindow:
    start_ms: int
    end_ms: int
    peak_score: float
    samples: int


@dataclass(frozen=True)
class ScreeningResult:
    compared_frames: int
    active_frames: int
    activity_ratio: float
    meaningful_activity: bool
    candidates: tuple[CandidateWindow, ...]


def screening_policy_from_profile(profile: object) -> ScreeningPolicy:
    raw = getattr(profile, "screening_policy_json", None)
    if not raw:
        policy = ScreeningPolicy()
        policy.validate()
        return policy
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError
        policy = ScreeningPolicy(
            frame_threshold=float(value.get("frame_threshold", 0.08)),
            min_active_ratio=float(value.get("min_active_ratio", 0.10)),
            sample_stride=int(value.get("sample_stride", 1)),
            merge_gap_ms=int(value.get("merge_gap_ms", 2_000)),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ScreeningError("SCREEN_POLICY_JSON_INVALID") from exc
    policy.validate()
    return policy


def grayscale_motion_score(previous: bytes, current: bytes) -> float:
    if not previous or not current or len(previous) != len(current):
        raise ScreeningError("SCREEN_FRAME_SHAPE_MISMATCH")
    total = sum(abs(a - b) for a, b in zip(previous, current))
    return total / (len(previous) * 255.0)


def _group_active_points(
    points: Sequence[tuple[int, float]],
    *,
    merge_gap_ms: int,
) -> tuple[CandidateWindow, ...]:
    if not points:
        return ()
    windows: list[CandidateWindow] = []
    start = last = points[0][0]
    peak = points[0][1]
    count = 1
    for timestamp_ms, score in points[1:]:
        if timestamp_ms - last <= merge_gap_ms:
            last = timestamp_ms
            peak = max(peak, score)
            count += 1
            continue
        windows.append(CandidateWindow(start_ms=start, end_ms=last, peak_score=peak, samples=count))
        start = last = timestamp_ms
        peak = score
        count = 1
    windows.append(CandidateWindow(start_ms=start, end_ms=last, peak_score=peak, samples=count))
    return tuple(windows)


def screen_samples(
    samples: Iterable[FrameSample],
    *,
    policy: ScreeningPolicy = ScreeningPolicy(),
) -> ScreeningResult:
    policy.validate()
    ordered = list(samples)
    if any(b.timestamp_ms <= a.timestamp_ms for a, b in zip(ordered, ordered[1:])):
        raise ScreeningError("SCREEN_TIMESTAMPS_NOT_STRICTLY_INCREASING")

    sampled = ordered[:: policy.sample_stride]
    if len(sampled) < 2:
        return ScreeningResult(
            compared_frames=0,
            active_frames=0,
            activity_ratio=0.0,
            meaningful_activity=False,
            candidates=(),
        )

    active_points: list[tuple[int, float]] = []
    compared = 0
    for previous, current in zip(sampled, sampled[1:]):
        score = grayscale_motion_score(previous.grayscale, current.grayscale)
        compared += 1
        if score >= policy.frame_threshold:
            active_points.append((current.timestamp_ms, score))

    active = len(active_points)
    ratio = active / compared if compared else 0.0
    return ScreeningResult(
        compared_frames=compared,
        active_frames=active,
        activity_ratio=ratio,
        meaningful_activity=ratio >= policy.min_active_ratio,
        candidates=_group_active_points(active_points, merge_gap_ms=policy.merge_gap_ms),
    )
