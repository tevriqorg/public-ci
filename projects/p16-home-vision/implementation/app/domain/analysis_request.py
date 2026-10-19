from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.analysis_contract import AnalysisRequest, EvidenceRef
from app.models import AnalysisArtifact, AnalysisProfile, AnalysisRun


class AnalysisRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _offsets(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    try:
        data: Any = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AnalysisRequestError("ANALYSIS_ARTIFACT_RANGE_INVALID") from exc
    if not isinstance(data, dict):
        raise AnalysisRequestError("ANALYSIS_ARTIFACT_RANGE_INVALID")
    start = data.get("start_offset_ms", data.get("offset_ms"))
    end = data.get("end_offset_ms", start)
    if start is not None and (not isinstance(start, int) or start < 0):
        raise AnalysisRequestError("ANALYSIS_ARTIFACT_RANGE_INVALID")
    if end is not None and (not isinstance(end, int) or end < 0):
        raise AnalysisRequestError("ANALYSIS_ARTIFACT_RANGE_INVALID")
    if start is not None and end is not None and end < start:
        raise AnalysisRequestError("ANALYSIS_ARTIFACT_RANGE_INVALID")
    return start, end


def build_analysis_request(
    session: Session,
    *,
    run: AnalysisRun,
) -> AnalysisRequest:
    profile = session.get(AnalysisProfile, (run.analysis_profile_id, run.analysis_profile_revision))
    if profile is None:
        raise AnalysisRequestError("ANALYSIS_PROFILE_NOT_FOUND")
    if profile.snapshot_hash != run.analysis_profile_snapshot_hash:
        raise AnalysisRequestError("ANALYSIS_PROFILE_PROVENANCE_MISMATCH")

    artifacts = session.scalars(
        select(AnalysisArtifact)
        .where(
            AnalysisArtifact.analysis_run_id == run.id,
            AnalysisArtifact.asset_id == run.asset_id,
        )
        .order_by(AnalysisArtifact.id)
    ).all()
    evidence: list[EvidenceRef] = []
    for artifact in artifacts:
        start, end = _offsets(artifact.offset_or_range_json)
        evidence.append(
            EvidenceRef(
                artifact_id=artifact.id,
                artifact_type=artifact.artifact_type,
                start_offset_ms=start,
                end_offset_ms=end,
            )
        )

    return AnalysisRequest(
        run_id=run.id,
        asset_id=run.asset_id,
        profile_id=profile.id,
        profile_revision=profile.revision,
        profile_snapshot_hash=profile.snapshot_hash,
        semantic_intent=profile.semantic_intent,
        output_schema_json=profile.prompt_schema_json,
        media_ref=f"home-vision://asset/{run.asset_id}",
        evidence=tuple(evidence),
    )
