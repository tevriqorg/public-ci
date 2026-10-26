from datetime import datetime, timezone
import hashlib
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.domain.reconciliation import reconcile_storage_files
from app.models import CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume
from scripts.p16_soak import enqueue_failures
from scripts.vela_soak_runtime import EvidenceStore, digest, record_daily_cycle
import json
import sqlite3
import math
import time


def test_s012_reports_storage_ledger_mismatches_without_mutating_rows_or_files(
    session, tmp_path: Path
):
    nvr = NvrDevice(id="s012-nvr", name="S012 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="s012-camera", nvr_id=nvr.id, track_id="202",
                         camera_name="S012 synthetic")
    volume = StorageVolume(
        id="s012-volume", node_id="fixture", mount_path_runtime=str(tmp_path),
        total_bytes=1024**4, free_bytes=900 * 1024**3, online=True, writable=True,
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.add(volume)
    session.flush()

    media_root = tmp_path / "recordings/2026/08/27/camera-202"
    deleted_path = media_root / "060001-source-deleted.mp4"
    clean_path = media_root / "060002-source-clean.mp4"
    file_only_path = media_root / "060003-untracked.mp4"
    deleted_path.parent.mkdir(parents=True)
    deleted_path.write_bytes(b"deleted-but-still-present")
    clean_path.write_bytes(b"clean-control-bytes")
    file_only_path.write_bytes(b"file-only-media-bytes")
    original_files = {
        path: path.read_bytes() for path in (deleted_path, clean_path, file_only_path)
    }

    starts = {
        "db-only": datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc),
        "deleted": datetime(2026, 8, 27, 6, 0, 1, tzinfo=timezone.utc),
        "clean": datetime(2026, 8, 27, 6, 0, 2, tzinfo=timezone.utc),
    }
    rows = []
    for suffix, start in starts.items():
        source = SourceRecording(
            id=f"s012-source-{suffix}", nvr_id=nvr.id, track_id=camera.track_id,
            source_start=start, source_end=start.replace(second=start.second + 10),
            duration_ms=10_000, source_state="available",
        )
        session.add(source)
        session.flush()
        relative_path = {
            "db-only": "recordings/2026/08/27/camera-202/060000-source-db-only.mp4",
            "deleted": deleted_path.relative_to(tmp_path).as_posix(),
            "clean": clean_path.relative_to(tmp_path).as_posix(),
        }[suffix]
        asset = LocalAsset(
            id=f"s012-asset-{suffix}", source_recording_id=source.id,
            storage_volume_id=volume.id, relative_path=relative_path,
            size_bytes=17, sha256="a" * 64, verification_state="verified",
            lifecycle_state="deleted" if suffix == "deleted" else "present",
        )
        session.add(asset)
        rows.append(asset)
    session.flush()

    row_snapshot = [
        (row.id, row.lifecycle_state, row.verification_state, row.relative_path)
        for row in rows
    ]

    clean_asset = next(row for row in rows if row.id == "s012-asset-clean")
    independent_sha = hashlib.sha256(clean_path.read_bytes()).hexdigest()
    assert independent_sha != clean_asset.sha256
    # C2 canary: a bad-hash mutant that echoes the ledger value must be killed
    # by an oracle derived independently from the synthetic media bytes.
    with patch(
        "app.domain.reconciliation._sha256_file", return_value=clean_asset.sha256
    ):
        mutant_result = reconcile_storage_files(session)
    assert not any(
        finding.code == "ASSET_SHA256_MISMATCH" for finding in mutant_result.findings
    )
    assert independent_sha != clean_asset.sha256

    result = reconcile_storage_files(session)

    assert {
        (finding.code, finding.relative_path, finding.asset_id)
        for finding in result.findings
    } == {
        ("DB_ONLY_ASSET", "recordings/2026/08/27/camera-202/060000-source-db-only.mp4",
         "s012-asset-db-only"),
        ("DELETED_FILE_PRESENT", deleted_path.relative_to(tmp_path).as_posix(),
         "s012-asset-deleted"),
        ("FILE_ONLY_MEDIA", file_only_path.relative_to(tmp_path).as_posix(), None),
        ("ASSET_SHA256_MISMATCH", clean_path.relative_to(tmp_path).as_posix(),
         "s012-asset-clean"),
        ("ASSET_SIZE_MISMATCH", clean_path.relative_to(tmp_path).as_posix(),
         "s012-asset-clean"),
    }
    assert not result.clean
    assert [
        (row.id, row.lifecycle_state, row.verification_state, row.relative_path)
        for row in rows
    ] == row_snapshot
    assert {path: path.read_bytes() for path in original_files} == original_files
    assert not session.new and not session.dirty and not session.deleted

    repeated = reconcile_storage_files(session)
    assert repeated == result
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_s012_reports_unreadable_recordings_subtree_instead_of_clean_result(
    session, tmp_path: Path, monkeypatch
):
    nvr = NvrDevice(id="s012-unreadable-nvr", name="S012 synthetic", host="127.0.0.1")
    session.add(nvr)
    session.add(StorageVolume(
        id="s012-unreadable-volume", node_id="fixture",
        mount_path_runtime=str(tmp_path), total_bytes=1024**4,
        free_bytes=900 * 1024**3, online=True, writable=True,
    ))
    session.flush()
    blocked = tmp_path / "recordings" / "unreadable-camera"
    blocked.mkdir(parents=True)
    (blocked / "untracked.mp4").write_bytes(b"synthetic hidden media")

    def walk_with_permission_fault(top, *, onerror, followlinks):
        assert Path(top) == tmp_path / "recordings"
        error = PermissionError(13, "synthetic permission fault", str(blocked))
        onerror(error)
        yield str(top), [blocked.name], []

    monkeypatch.setattr("app.domain.reconciliation.os.walk", walk_with_permission_fault)
    result = reconcile_storage_files(session)

    assert not result.clean
    assert {
        (finding.code, finding.relative_path)
        for finding in result.findings
    } == {("RECORDINGS_SUBTREE_UNREADABLE", "recordings/unreadable-camera")}


def test_s032_reconciles_fifteen_day_media_evidence_and_source_history(session, tmp_path: Path):
    """One replay audits historical media, daily outcomes, failures, and revisions."""
    nvr = NvrDevice(id="s032-history-nvr", name="S032 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="s032-history-camera", nvr_id=nvr.id, track_id="202",
                         camera_name="S032 synthetic")
    volume = StorageVolume(
        id="s032-history-volume", node_id="fixture", mount_path_runtime=str(tmp_path),
        total_bytes=1024**4, free_bytes=900 * 1024**3, online=True, writable=True,
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.add(volume)
    session.flush()

    first_day = datetime.now(timezone.utc).date() - timedelta(days=14)
    snapshots = {}
    expected_hashes = {}
    for offset in range(15):
        day = first_day + timedelta(days=offset)
        payload = f"synthetic historical video {day.isoformat()}".encode()
        relative = Path("recordings") / day.isoformat() / "camera-202" / "segment.mp4"
        media_path = tmp_path / relative
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(payload)
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        source = SourceRecording(
            id=f"s032-history-source-{offset:02d}", nvr_id=nvr.id,
            track_id=camera.track_id, source_start=start,
            source_end=start + timedelta(seconds=30), duration_ms=30_000,
            source_state="available",
        )
        session.add(source)
        session.flush()
        session.add(LocalAsset(
            id=f"s032-history-asset-{offset:02d}", source_recording_id=source.id,
            storage_volume_id=volume.id, relative_path=relative.as_posix(),
            size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            verification_state="verified", lifecycle_state="present",
        ))
        snapshots[media_path] = payload
        expected_hashes[media_path] = hashlib.sha256(payload).hexdigest()
    session.flush()

    rows_before = [
        (asset.id, asset.relative_path, asset.size_bytes, asset.sha256,
         asset.lifecycle_state, asset.verification_state)
        for asset in session.query(LocalAsset).filter_by(storage_volume_id=volume.id).all()
    ]
    assert len(rows_before) == 15
    sources_before = [
        (source.id, source.track_id, source.source_start, source.source_end,
         source.duration_ms, source.source_state)
        for source in session.query(SourceRecording).filter_by(nvr_id=nvr.id).order_by(
            SourceRecording.id
        ).all()
    ]
    assert len(sources_before) == 15
    for path, payload in snapshots.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hashes[path]

    # Produce a durable date-indexed evidence history. Source SHAs represent
    # successive synthetic code revisions; a failed day is replayed after a
    # revision, while both immutable runs and the original daily FAIL survive.
    state = tmp_path / "s032-state"
    store = EvidenceStore(state)
    scheduled_start = first_day.isoformat()
    evidence_rows = []
    failed_day = first_day + timedelta(days=3)
    for offset in range(15):
        day = first_day + timedelta(days=offset)
        source_sha = hashlib.sha256(f"synthetic-source-{offset}".encode()).hexdigest()[:40]
        status = "FAIL" if day == failed_day else "PASS"
        run_id = f"s032-day-{day.isoformat()}-original"
        at = datetime.combine(day, datetime.min.time().replace(hour=12), tzinfo=timezone.utc)
        daily = record_daily_cycle(
            state, scheduled_start=scheduled_start, observed_day=day.isoformat(),
            run_id=run_id, complete=status == "PASS", observed_at=at.isoformat(),
        )
        assert daily["attempt"] == (run_id, day.isoformat(), status)
        record = {
            "run_id": run_id, "source_sha": source_sha, "scenario_id": "S032",
            "seed": "p16-g0-v1-S032", "timestamp_utc": at.isoformat(),
            "status": status, "history_day": day.isoformat(),
        }
        store.append(record)
        enqueue_failures(state, [record])
        evidence_rows.append(record)
        if day == failed_day:
            fixed_sha = hashlib.sha256(b"synthetic-fixed-source-revision").hexdigest()[:40]
            recovery_id = f"s032-day-{day.isoformat()}-recovery"
            recovery = {
                **record, "run_id": recovery_id, "source_sha": fixed_sha,
                "timestamp_utc": (at + timedelta(hours=1)).isoformat(),
                "status": "PASS", "recovery_of": run_id,
            }
            recovery_daily = record_daily_cycle(
                state, scheduled_start=scheduled_start, observed_day=day.isoformat(),
                run_id=recovery_id, complete=True,
                observed_at=(at + timedelta(hours=1)).isoformat(),
            )
            assert recovery_daily["attempt"] == (recovery_id, day.isoformat(), "PASS")
            store.append(recovery)
            enqueue_failures(state, [recovery])
            evidence_rows.append(recovery)

    persisted = store.records()
    assert len(persisted) == 16
    assert {row["run_id"] for row in persisted} == {row["run_id"] for row in evidence_rows}
    assert all(digest(record) == next(
        path.stem for path in (state / "runs").glob("*.json")
        if json.loads(path.read_text()) == record
    ) for record in persisted)
    assert len({record["source_sha"] for record in persisted}) == 16
    with sqlite3.connect(state / "daily-ledger.sqlite3") as db:
        schedule = db.execute(
            "SELECT day, state FROM daily_schedule ORDER BY day"
        ).fetchall()
        attempts = db.execute(
            "SELECT day, outcome FROM daily_attempts ORDER BY observed_at, run_id"
        ).fetchall()
    assert [day for day, _ in schedule] == [
        (first_day + timedelta(days=offset)).isoformat() for offset in range(15)
    ]
    assert [state for _, state in schedule] == [
        "FAIL" if day == failed_day.isoformat() else "PASS" for day, _ in schedule
    ]
    assert len(attempts) == 16
    assert [outcome for day, outcome in attempts if day == failed_day.isoformat()] == [
        "FAIL", "PASS",
    ]
    with sqlite3.connect(state / "repair-queue.sqlite3") as db:
        tasks = db.execute("SELECT scenario, seed, source, status FROM tasks").fetchall()
        observations = db.execute("SELECT run_id, evidence_hash FROM observations").fetchall()
    assert tasks == [("S032", "p16-g0-v1-S032",
                      next(row["source_sha"] for row in evidence_rows
                           if row["history_day"] == failed_day.isoformat()),
                      "NEEDS_REPRO_AND_FIX")]
    assert len(observations) == 1
    assert observations[0][1] == digest(next(
        row for row in evidence_rows if row["status"] == "FAIL"
    ))

    # Persist and independently recompute a date-indexed hash chain over every
    # immutable run, including the old failure and its later source revision.
    previous = None
    chain_rows = []
    for record in sorted(persisted, key=lambda row: (
        row["history_day"], row["timestamp_utc"], row["run_id"]
    )):
        entry = {
            "history_day": record["history_day"], "run_id": record["run_id"],
            "source_sha": record["source_sha"], "evidence_sha256": digest(record),
            "previous_sha256": previous,
        }
        chain_sha = digest(entry)
        chain_rows.append({**entry, "chain_sha256": chain_sha})
        previous = chain_sha
    chain_path = state / "history-chain.jsonl"
    with chain_path.open("xb") as chain_file:
        chain_file.write(b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            for row in chain_rows
        ))
        chain_file.flush()
    chain_snapshot = chain_path.read_bytes()
    previous = None
    verified_chain = []
    for line in chain_snapshot.splitlines():
        row = json.loads(line)
        chain_sha = row.pop("chain_sha256")
        assert row["previous_sha256"] == previous
        assert chain_sha == digest(row)
        assert row["evidence_sha256"] == next(
            digest(record) for record in persisted if record["run_id"] == row["run_id"]
        )
        assert row["source_sha"] == next(
            record["source_sha"] for record in persisted
            if record["run_id"] == row["run_id"]
        )
        previous = chain_sha
        verified_chain.append(chain_sha)
    assert len(verified_chain) == 16
    assert verified_chain == [row["chain_sha256"] for row in chain_rows]

    # Sample a full-history reconciliation for each of the 15 calendar dates;
    # compute p95 from observed monotonic timings and keep the sample immutable.
    samples = []
    for offset in range(15):
        day = first_day + timedelta(days=offset)
        started = time.perf_counter()
        sampled = reconcile_storage_files(session)
        elapsed = max(time.perf_counter() - started, 1e-9)
        assert sampled.clean
        samples.append({"day": day.isoformat(), "elapsed_seconds": elapsed})
    p95 = sorted(sample["elapsed_seconds"] for sample in samples)[math.ceil(.95 * len(samples)) - 1]
    assert len(samples) == 15 and math.isfinite(p95) and p95 > 0
    sample_path = state / "history-p95.json"
    sample_snapshot = json.dumps(
        {"samples": samples, "p95_seconds": p95}, sort_keys=True, separators=(",", ":")
    ).encode()
    with sample_path.open("xb") as sample_file:
        sample_file.write(sample_snapshot)
        sample_file.flush()
    sample_record = json.loads(sample_path.read_bytes())
    assert [sample["day"] for sample in sample_record["samples"]] == [
        (first_day + timedelta(days=offset)).isoformat() for offset in range(15)
    ]
    assert sample_record["p95_seconds"] == p95
    assert all(math.isfinite(sample["elapsed_seconds"]) and sample["elapsed_seconds"] > 0
               for sample in sample_record["samples"])

    # Mutate a past file without changing size; an independent bytes-based SHA
    # oracle must identify exactly the affected historical ledger row.
    mutated_path = sorted(snapshots)[3]
    original_bytes = mutated_path.read_bytes()
    mutated_bytes = bytes([original_bytes[0] ^ 1]) + original_bytes[1:]
    assert len(mutated_bytes) == len(original_bytes)
    mutated_path.write_bytes(mutated_bytes)
    independent_mutated_sha = hashlib.sha256(mutated_path.read_bytes()).hexdigest()
    assert independent_mutated_sha != expected_hashes[mutated_path]

    changed = reconcile_storage_files(session)
    mismatch = [finding for finding in changed.findings
                if finding.code == "ASSET_SHA256_MISMATCH"]
    assert [(finding.relative_path, finding.asset_id) for finding in mismatch] == [
        (mutated_path.relative_to(tmp_path).as_posix(), f"s032-history-asset-03")
    ]
    assert not any(finding.code == "ASSET_SIZE_MISMATCH" for finding in changed.findings)
    assert mutated_path.read_bytes() == mutated_bytes
    assert [
        (asset.id, asset.relative_path, asset.size_bytes, asset.sha256,
         asset.lifecycle_state, asset.verification_state)
        for asset in session.query(LocalAsset).filter_by(storage_volume_id=volume.id).all()
    ] == rows_before
    assert [
        (source.id, source.track_id, source.source_start, source.source_end,
         source.duration_ms, source.source_state)
        for source in session.query(SourceRecording).filter_by(nvr_id=nvr.id).order_by(
            SourceRecording.id
        ).all()
    ] == sources_before
    assert chain_path.read_bytes() == chain_snapshot
    assert sample_path.read_bytes() == sample_snapshot
    assert [
        (source.id, source.track_id, source.source_start, source.source_end,
         source.duration_ms, source.source_state)
        for source in session.query(SourceRecording).filter_by(nvr_id=nvr.id).order_by(
            SourceRecording.id
        ).all()
    ] == sources_before
    assert chain_path.read_bytes() == chain_snapshot
    assert sample_path.read_bytes() == sample_snapshot

    mutated_path.write_bytes(original_bytes)
    restored = reconcile_storage_files(session)
    assert restored.clean
    assert len([path for path in (tmp_path / "recordings").rglob("*.mp4")]) == 15
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == expected_hashes[path]
               for path in snapshots)
    assert [
        (asset.id, asset.relative_path, asset.size_bytes, asset.sha256,
         asset.lifecycle_state, asset.verification_state)
        for asset in session.query(LocalAsset).filter_by(storage_volume_id=volume.id).all()
    ] == rows_before
