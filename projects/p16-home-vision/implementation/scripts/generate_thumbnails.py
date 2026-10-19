from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.domain.media_access import resolve_media_path
from app.domain.storage import StoragePolicy
from app.domain.thumbnails import ThumbnailError, generate_thumbnail, thumbnail_path
from app.models import LocalAsset, SourceRecording, StorageVolume


THUMBNAIL_WRITE_RESERVE_BYTES = 5 * 1024**2


def _parse_date(value: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _ensure_write_boundary(volume: StorageVolume) -> None:
    if not volume.online or not volume.writable:
        raise ThumbnailError("THUMBNAIL_STORAGE_NOT_WRITABLE")
    usage = shutil.disk_usage(volume.mount_path_runtime)
    reserve = StoragePolicy().reserve_for(usage.total) + THUMBNAIL_WRITE_RESERVE_BYTES
    if usage.free <= reserve:
        raise ThumbnailError("THUMBNAIL_STORAGE_RESERVE")


def _select_rows(args: argparse.Namespace) -> list[tuple[LocalAsset, SourceRecording, StorageVolume]]:
    with SessionLocal() as session:
        statement = (
            select(LocalAsset, SourceRecording, StorageVolume)
            .join(SourceRecording, SourceRecording.id == LocalAsset.source_recording_id)
            .join(StorageVolume, StorageVolume.id == LocalAsset.storage_volume_id)
            .where(
                LocalAsset.asset_role == "source_archive",
                LocalAsset.verification_state == "verified",
                LocalAsset.lifecycle_state == "present",
            )
            .order_by(SourceRecording.source_start, LocalAsset.id)
        )
        if args.date:
            start, end = _parse_date(args.date)
            statement = statement.where(SourceRecording.source_start >= start, SourceRecording.source_start < end)
        if args.asset_id:
            statement = statement.where(LocalAsset.id.in_(args.asset_id))
        rows = session.execute(statement).all()
        return list(rows)


def run(args: argparse.Namespace) -> int:
    rows = _select_rows(args)
    print(f"selected={len(rows)}")
    created = already_present = failed = 0
    with SessionLocal() as session:
        for asset, _source, volume in rows[: args.limit or None]:
            try:
                output_path = thumbnail_path(Path(volume.mount_path_runtime), asset.id)
                if output_path.is_file() and output_path.stat().st_size > 0:
                    result = "already_present"
                else:
                    _ensure_write_boundary(volume)
                    source_path = resolve_media_path(Path(volume.mount_path_runtime), asset.relative_path)
                    result = generate_thumbnail(source_path, output_path)
                if result == "created":
                    created += 1
                else:
                    already_present += 1
                print(f"asset={asset.id} result={result} path={output_path}", flush=True)
            except (ThumbnailError, OSError, ValueError) as exc:
                failed += 1
                print(f"asset={asset.id} result=failed error={getattr(exc, 'code', type(exc).__name__)}", flush=True)
        session.rollback()
    print(f"created={created} already_present={already_present} failed={failed}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Create persistent same-volume Home Vision thumbnails")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="process every verified present source archive")
    scope.add_argument("--date", help="process one UTC source date, YYYY-MM-DD")
    scope.add_argument("--asset-id", action="append", help="process one asset; repeat for a bounded list")
    parser.add_argument("--limit", type=int, help="maximum number of selected assets")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
