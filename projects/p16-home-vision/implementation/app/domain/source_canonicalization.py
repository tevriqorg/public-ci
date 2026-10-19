from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ArchiveJob, ArchiveSourceSnapshot, AuditEvent, DeleteIntent, LocalAsset, SourceRecording, Tombstone, utcnow


class CanonicalizationConflict(RuntimeError):
    pass


@dataclass
class CanonicalizationGroup:
    identity: tuple[str, str, datetime]
    source_ids: tuple[str, ...]
    canonical_source_id: str | None
    redundant_source_ids: tuple[str, ...] = ()
    reparent_counts: dict[str, int] = field(default_factory=dict)
    conflict: str | None = None


@dataclass
class CanonicalizationResult:
    month: str
    groups: list[CanonicalizationGroup]
    applied: bool


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        year, month_number = (int(value) for value in month.split("-"))
        start = datetime(year, month_number, 1, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("month must be YYYY-MM") from None
    end = datetime(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1, tzinfo=timezone.utc)
    return start, end


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _archive_content_proofs(session: Session, source_id: str) -> set[tuple[str, int]]:
    assets = session.scalars(select(LocalAsset).where(
        LocalAsset.source_recording_id == source_id,
        LocalAsset.asset_role == "source_archive",
        LocalAsset.verification_state == "verified",
        LocalAsset.lifecycle_state == "present",
    )).all()
    return {(asset.sha256, asset.size_bytes) for asset in assets if asset.sha256 and asset.size_bytes is not None}


def canonicalize_sources(session: Session, *, month: str = "2026-09", apply: bool = False, allow_conflicts: bool = False) -> CanonicalizationResult:
    start, end = _month_bounds(month)
    sources = session.scalars(
        select(SourceRecording).where(SourceRecording.source_start >= start, SourceRecording.source_start < end).order_by(SourceRecording.source_start, SourceRecording.id)
    ).all()
    grouped: dict[tuple[str, str, datetime], list[SourceRecording]] = {}
    for source in sources:
        grouped.setdefault((source.nvr_id, source.track_id, source.source_start), []).append(source)

    results: list[CanonicalizationGroup] = []
    for identity, rows in grouped.items():
        if len(rows) < 2:
            continue
        anchored: list[SourceRecording] = []
        for source in rows:
            has_archive = session.scalar(select(LocalAsset.id).where(
                LocalAsset.source_recording_id == source.id,
                LocalAsset.asset_role == "source_archive",
                LocalAsset.verification_state == "verified",
                LocalAsset.lifecycle_state == "present",
            )) is not None
            has_tombstone = session.scalar(select(Tombstone.id).where(
                Tombstone.source_recording_id == source.id,
                Tombstone.suppress_redownload.is_(True),
            )) is not None
            if has_archive or has_tombstone:
                anchored.append(source)
        conflict = None
        canonical: SourceRecording | None = None
        if len(anchored) > 1:
            proofs = [_archive_content_proofs(session, source.id) for source in anchored]
            if all(proof and proof == proofs[0] for proof in proofs[1:]):
                canonical = max(anchored, key=lambda item: (_utc(item.source_end), item.id))
            else:
                conflict = "multiple archive anchors"
        elif anchored:
            canonical = anchored[0]
        else:
            ordered = sorted(rows, key=lambda item: (_utc(item.source_end), item.id), reverse=True)
            if len(ordered) > 1 and _utc(ordered[0].source_end) == _utc(ordered[1].source_end):
                conflict = "equal source_end without archive anchor"
            else:
                canonical = ordered[0]
        redundant = tuple(row.id for row in rows if canonical is not None and row.id != canonical.id)
        group = CanonicalizationGroup(identity, tuple(row.id for row in rows), canonical.id if canonical else None, redundant, conflict=conflict)
        results.append(group)

    conflicts = [group for group in results if group.conflict]
    if apply and conflicts and not allow_conflicts:
        raise CanonicalizationConflict("; ".join(f"{group.identity}: {group.conflict}" for group in conflicts))
    if not apply:
        return CanonicalizationResult(month, results, False)

    for group in results:
        if group.canonical_source_id is None:
            continue
        canonical_id = group.canonical_source_id
        counts: dict[str, int] = {}
        for model, field_name in (
            (LocalAsset, "source_recording_id"),
            (ArchiveJob, "source_recording_id"),
            (ArchiveSourceSnapshot, "source_recording_id"),
            (DeleteIntent, "source_recording_id"),
            (Tombstone, "source_recording_id"),
        ):
            rows = session.scalars(select(model).where(getattr(model, field_name).in_(group.redundant_source_ids))).all()
            for row in rows:
                setattr(row, field_name, canonical_id)
            if rows:
                counts[model.__tablename__] = len(rows)
        session.add(AuditEvent(
            id=f"audit-canonicalize-{canonical_id}",
            event_type="source_recordings_canonicalized",
            entity_type="source_recording",
            entity_id=canonical_id,
            actor="migration-gate",
            payload_redacted=f"month={month};redundant={len(group.redundant_source_ids)}",
        ))
        for source_id in group.redundant_source_ids:
            source = session.get(SourceRecording, source_id)
            if source is not None:
                session.delete(source)
        group.reparent_counts = counts
    session.flush()
    return CanonicalizationResult(month, results, True)
