from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from app.domain.manifest import upsert_source_recording
from app.infra.nvr.hikvision import DiscoveredSegment
from app.models import SourceRecording


@dataclass(frozen=True)
class ReconciliationResult:
    source_ids: tuple[str, ...]
    discovered_count: int


def reconcile_segments(
    session: Session,
    *,
    nvr_id: str,
    segments: Iterable[DiscoveredSegment],
    scan_id: str | None,
) -> ReconciliationResult:
    ids: list[str] = []
    count = 0
    for segment in segments:
        recording = upsert_source_recording(
            session,
            nvr_id=nvr_id,
            track_id=segment.track_id,
            source_start=segment.source_start,
            source_end=segment.source_end,
            duration_ms=segment.duration_ms,
            locator_fingerprint=segment.locator_fingerprint,
            source_size_if_known=segment.source_size_if_known,
            scan_id=scan_id,
        )
        recording.content_type = segment.content_type
        recording.recording_type = segment.recording_type
        ids.append(recording.id)
        count += 1
    session.flush()
    return ReconciliationResult(tuple(ids), count)


def scan_state_advances_expiry(state: str) -> bool:
    return state == "success"


def should_mark_expired(
    recording: SourceRecording,
    *,
    successful_covering_misses: int,
    current_write_edge: datetime,
    guard: timedelta = timedelta(hours=1),
) -> bool:
    """Conservative R6 rule: two successful misses and clearly behind write edge."""
    if successful_covering_misses < 2:
        return False
    source_end = recording.source_end
    if source_end.tzinfo is None:
        source_end = source_end.replace(tzinfo=current_write_edge.tzinfo)
    return source_end <= current_write_edge - guard
