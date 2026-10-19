from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.analysis_contract import (
    AnalysisRequest,
    AnalysisResult,
    EventResult,
    ModelRunner,
    ModelRunnerError,
    RunnerIdentity,
)
from app.models import AnalysisArtifact, AnalysisEvent, AnalysisRun


class AnalysisResultError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _event_id(run_id: str, index: int, event: EventResult) -> str:
    payload = json.dumps(
        {
            "run_id": run_id,
            "index": index,
            "start_offset_ms": event.start_offset_ms,
            "end_offset_ms": event.end_offset_ms,
            "event_type": event.event_type,
            "summary": event.summary,
            "confidence": event.confidence,
            "evidence_artifact_ids": sorted(event.evidence_artifact_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"event-{digest}"


def _validate_event(event: EventResult) -> None:
    if event.start_offset_ms < 0:
        raise AnalysisResultError("ANALYSIS_EVENT_OFFSET_INVALID")
    if event.end_offset_ms is not None and event.end_offset_ms < event.start_offset_ms:
        raise AnalysisResultError("ANALYSIS_EVENT_OFFSET_INVALID")
    if event.confidence is not None and not 0 <= event.confidence <= 1:
        raise AnalysisResultError("ANALYSIS_EVENT_CONFIDENCE_INVALID")
    if not event.event_type.strip() or not event.summary.strip():
        raise AnalysisResultError("ANALYSIS_EVENT_TEXT_INVALID")


def _validate_evidence(
    session: Session,
    *,
    run: AnalysisRun,
    result: AnalysisResult,
) -> None:
    referenced = {artifact_id for event in result.events for artifact_id in event.evidence_artifact_ids}
    if not referenced:
        return
    rows = session.scalars(select(AnalysisArtifact).where(AnalysisArtifact.id.in_(referenced))).all()
    by_id = {row.id: row for row in rows}
    if set(by_id) != referenced:
        raise AnalysisResultError("ANALYSIS_EVIDENCE_NOT_FOUND")
    if any(row.analysis_run_id != run.id or row.asset_id != run.asset_id for row in rows):
        raise AnalysisResultError("ANALYSIS_EVIDENCE_FOREIGN")


def validate_result(
    session: Session,
    *,
    run: AnalysisRun,
    request: AnalysisRequest,
    result: AnalysisResult,
) -> None:
    if request.run_id != run.id or request.asset_id != run.asset_id:
        raise AnalysisResultError("ANALYSIS_REQUEST_IDENTITY_MISMATCH")
    if result.run_id != run.id or result.asset_id != run.asset_id:
        raise AnalysisResultError("ANALYSIS_RESULT_IDENTITY_MISMATCH")
    if request.profile_id != run.analysis_profile_id:
        raise AnalysisResultError("ANALYSIS_REQUEST_PROFILE_MISMATCH")
    if request.profile_revision != run.analysis_profile_revision:
        raise AnalysisResultError("ANALYSIS_REQUEST_PROFILE_MISMATCH")
    if request.profile_snapshot_hash != run.analysis_profile_snapshot_hash:
        raise AnalysisResultError("ANALYSIS_REQUEST_PROFILE_MISMATCH")
    for event in result.events:
        _validate_event(event)
    _validate_evidence(session, run=run, result=result)


def persist_result(
    session: Session,
    *,
    run: AnalysisRun,
    request: AnalysisRequest,
    identity: RunnerIdentity,
    result: AnalysisResult,
) -> list[AnalysisEvent]:
    validate_result(session, run=run, request=request, result=result)
    created: list[AnalysisEvent] = []
    for index, event in enumerate(result.events):
        event_id = _event_id(run.id, index, event)
        row = session.get(AnalysisEvent, event_id)
        if row is None:
            row = AnalysisEvent(
                id=event_id,
                analysis_run_id=run.id,
                asset_id=run.asset_id,
                start_offset_ms=event.start_offset_ms,
                end_offset_ms=event.end_offset_ms,
                event_type=event.event_type,
                summary=event.summary,
                confidence=event.confidence,
                evidence_refs_json=json.dumps(
                    list(event.evidence_artifact_ids),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            session.add(row)
        created.append(row)

    run.model_id = identity.model_id
    run.model_version = identity.model_version
    run.runtime_backend = identity.runtime_backend
    run.state = "completed"
    run.completed_at = _utcnow()
    run.error = None
    session.flush()
    return created


def record_failure(session: Session, *, run: AnalysisRun, code: str) -> None:
    safe = code.strip().upper().replace(" ", "_")[:128] or "ANALYSIS_FAILED"
    run.state = "failed"
    run.completed_at = _utcnow()
    run.error = safe
    session.flush()


def execute_runner(
    session: Session,
    *,
    run: AnalysisRun,
    request: AnalysisRequest,
    runner: ModelRunner,
) -> list[AnalysisEvent]:
    try:
        result = runner.analyze(request)
        return persist_result(
            session,
            run=run,
            request=request,
            identity=runner.identity(),
            result=result,
        )
    except ModelRunnerError as exc:
        record_failure(session, run=run, code=exc.code)
        raise
    except Exception:
        record_failure(session, run=run, code="ANALYSIS_RUNNER_UNEXPECTED")
        raise
