from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RunnerIdentity:
    model_id: str
    model_version: str
    runtime_backend: str


@dataclass(frozen=True)
class EvidenceRef:
    artifact_id: str
    artifact_type: str
    start_offset_ms: int | None = None
    end_offset_ms: int | None = None


@dataclass(frozen=True)
class AnalysisRequest:
    run_id: str
    asset_id: str
    profile_id: str
    profile_revision: int
    profile_snapshot_hash: str
    semantic_intent: str | None
    output_schema_json: str | None
    media_ref: str
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class EventResult:
    start_offset_ms: int
    end_offset_ms: int | None
    event_type: str
    summary: str
    confidence: float | None = None
    evidence_artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisResult:
    run_id: str
    asset_id: str
    events: tuple[EventResult, ...]


class ModelRunnerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@runtime_checkable
class ModelRunner(Protocol):
    def identity(self) -> RunnerIdentity:
        ...

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        ...
