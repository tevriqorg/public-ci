from __future__ import annotations

from dataclasses import dataclass

from app.domain.analysis_contract import (
    AnalysisRequest,
    AnalysisResult,
    EventResult,
    ModelRunnerError,
    RunnerIdentity,
)


@dataclass(frozen=True)
class FixtureEventTemplate:
    start_offset_ms: int
    end_offset_ms: int | None
    event_type: str
    summary: str
    confidence: float | None = None
    evidence_artifact_ids: tuple[str, ...] = ()


class FixtureModelRunner:
    """Deterministic test runner. This is not a production model baseline."""

    def __init__(
        self,
        *,
        events: tuple[FixtureEventTemplate, ...] = (),
        model_id: str = "fixture-model",
        model_version: str = "fixture-v1",
        runtime_backend: str = "fixture",
    ) -> None:
        self._events = events
        self._identity = RunnerIdentity(
            model_id=model_id,
            model_version=model_version,
            runtime_backend=runtime_backend,
        )

    def identity(self) -> RunnerIdentity:
        return self._identity

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        return AnalysisResult(
            run_id=request.run_id,
            asset_id=request.asset_id,
            events=tuple(
                EventResult(
                    start_offset_ms=item.start_offset_ms,
                    end_offset_ms=item.end_offset_ms,
                    event_type=item.event_type,
                    summary=item.summary,
                    confidence=item.confidence,
                    evidence_artifact_ids=item.evidence_artifact_ids,
                )
                for item in self._events
            ),
        )


class FailingFixtureModelRunner:
    """Deterministic failure runner used to prove failure isolation."""

    def __init__(self, code: str = "FIXTURE_MODEL_FAILED") -> None:
        self._code = code

    def identity(self) -> RunnerIdentity:
        return RunnerIdentity(
            model_id="fixture-failing-model",
            model_version="fixture-v1",
            runtime_backend="fixture",
        )

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        del request
        raise ModelRunnerError(self._code)
