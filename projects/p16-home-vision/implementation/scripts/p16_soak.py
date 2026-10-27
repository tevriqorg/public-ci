"""Candidate executable registry and durable repair queue; never grants day credit."""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
import shutil
import subprocess
from pathlib import Path
from vela_soak_runtime import EvidenceStore, command_handler, continuous, digest, run_cycle, run_scenario

ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"^[0-9a-f]{40}$")


def verify_source_revision(root, source_sha):
    """Require the evidence revision to identify this clean executable tree."""
    root = Path(root)
    try:
        repository = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ValueError("SOURCE_REVISION_UNAVAILABLE") from None
    repository = Path(repository).resolve()
    root = root.resolve()
    prefix = root.relative_to(repository).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    revision_candidates = [root / "SOURCE_REVISION"]
    if root.name == "implementation":
        revision_candidates.append(root.parent / "SOURCE_REVISION")
    mapped_revision = next((path for path in revision_candidates if path.is_file()), None)
    expected_revision = head
    if mapped_revision is not None:
        try:
            marker = mapped_revision.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise ValueError("SOURCE_REVISION_UNAVAILABLE") from None
        if "=" in marker:
            revisions = [line.partition("=")[2] for line in marker.splitlines()
                         if line.startswith("commit=")]
            if len(revisions) != 1:
                raise ValueError("SOURCE_REVISION_UNAVAILABLE")
            expected_revision = revisions[0]
        else:
            expected_revision = marker
    if expected_revision != source_sha:
        raise ValueError("SOURCE_SHA_DOES_NOT_MATCH_CHECKOUT")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=repository, check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError("SOURCE_REVISION_UNAVAILABLE") from None
    if root.name == "implementation":
        implementation = prefix.rstrip("/") + "/"
        # The canonical source checkout stores design beside implementation;
        # the public mirror stores the same catalog beside its implementation.
        project_prefix = prefix.rstrip("/").rpartition("/")[0]
        if project_prefix:
            project_prefix += "/"
        catalog_paths = {project_prefix + "design/P16R2_VELA_SOAK_G0_SCENARIOS_v1.json"}
        marker_paths = {project_prefix + "SOURCE_REVISION"}
    else:
        implementation = prefix + "implementation/"
        catalog_paths = {prefix + "design/P16R2_VELA_SOAK_G0_SCENARIOS_v1.json"}
        marker_paths = {prefix + "SOURCE_REVISION"}
    for entry in status.split(b"\0"):
        if not entry:
            continue
        path = entry[3:] if len(entry) >= 3 and entry[2:3] == b" " else entry
        implementation_bytes = implementation.encode("utf-8")
        catalog_bytes = {candidate.encode("utf-8") for candidate in catalog_paths}
        marker_bytes = {candidate.encode("utf-8") for candidate in marker_paths}
        if path == implementation_bytes + b".tmp" or path.startswith(implementation_bytes + b".tmp/"):
            continue
        if path.startswith(implementation_bytes) or path in catalog_bytes or path in marker_bytes:
            raise ValueError("SOURCE_TREE_DIRTY")
    return expected_revision


def _validate_failure(record):
    required = ("run_id", "scenario_id", "seed", "source_sha", "status")
    if not isinstance(record, dict) or any(not isinstance(record.get(k), str) or not record[k] for k in required):
        raise ValueError("INVALID_FAILURE_RECORD")
    if record["status"] not in ("PASS", "FAIL", "BLOCKED"):
        raise ValueError("INVALID_FAILURE_STATUS")
    if not SHA.fullmatch(record["source_sha"]):
        raise ValueError("INVALID_FAILURE_SOURCE_SHA")


def enqueue_failures(root, records):
    """Deduplicate same-version reproductions, retain every evidence reference."""
    root = Path(root).absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError('REPAIR_STATE_SYMLINK_FORBIDDEN')
    if root.exists() and not root.is_dir():
        raise ValueError('REPAIR_STATE_NOT_DIRECTORY')
    root.mkdir(parents=True, exist_ok=True)
    queue_path = root / 'repair-queue.sqlite3'
    if queue_path.is_symlink():
        raise ValueError('REPAIR_QUEUE_SYMLINK_FORBIDDEN')
    failures = []
    observations = []
    batch_run_ids = {}
    # Validate the complete batch before opening a write transaction. A malformed
    # record must not leave even a partially initialized repair queue behind.
    for r in records:
        _validate_failure(r)
        key = digest([r['scenario_id'], r['seed'], r['source_sha']])
        evidence_hash = digest(r)
        previous = batch_run_ids.setdefault(r['run_id'], (key, evidence_hash))
        if previous != (key, evidence_hash):
            raise ValueError('REPAIR_OBSERVATION_CONFLICT')
        observations.append((r['run_id'], key, evidence_hash))
        if r['status'] != 'FAIL':
            continue
        failures.append((key, r, evidence_hash))

    with sqlite3.connect(queue_path) as db:
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS tasks (key TEXT PRIMARY KEY, scenario TEXT, seed TEXT, source TEXT, status TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS observations (run_id TEXT PRIMARY KEY, task_key TEXT, evidence_hash TEXT)')
        # Every terminal status is an immutable observation. Persisting PASS and
        # BLOCKED as well as FAIL prevents a later contradictory replay from
        # depending on which status happened to arrive first.
        for run_id, key, evidence_hash in observations:
            prior = db.execute('SELECT task_key, evidence_hash FROM observations WHERE run_id=?', (run_id,)).fetchone()
            if prior and prior != (key, evidence_hash):
                raise ValueError('REPAIR_OBSERVATION_CONFLICT')
        db.executemany('INSERT OR IGNORE INTO observations VALUES (?,?,?)', observations)
        for key, r, evidence_hash in failures:
            db.execute('INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,?)',
                       (key, r['scenario_id'], r['seed'], r['source_sha'], 'NEEDS_REPRO_AND_FIX'))


def resource_guard(state, *, max_bytes=64*1024**2,
                   min_free_bytes=6*1024**3, disk_usage=None):
    """Fail closed before a scenario when its evidence or host budget is unsafe."""
    usage = disk_usage or shutil.disk_usage
    root = Path(state)
    size = 0
    for item in root.rglob('*'):
        if item.is_symlink():
            raise OSError('EVIDENCE_SYMLINK_FORBIDDEN')
        if item.is_file():
            size += item.stat().st_size
    if size > max_bytes:
        raise OSError('EVIDENCE_BUDGET_EXHAUSTED')
    if usage(root).free < min_free_bytes:
        raise OSError('HOST_FREE_BELOW_LIMIT')


def registry():
    # Each registered test contains product assertions; unimplemented scenarios
    # deliberately remain absent, never credited by generic regression results.
    return {
        'S001': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s001_two_camera_closed_day_archives_only_closed_segments'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S002': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s002_cross_midnight_growth_waits_for_stable_closed_day_scans'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S003': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s003_three_day_two_camera_backfill_has_no_archive_omissions'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S004': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s004_repeated_scan_enqueues_one_verified_asset'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S005': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s005_http_453_is_retryable_only_after_explicit_backoff'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S006': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s006_download_faults_quarantine_before_bounded_retry'],
            cwd=ROOT/'implementation',timeout_seconds=180,expected_test_count=3),
        'S007': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_archive_worker_recovery.py::test_s007_sigkill_after_file_promotion_recovers_without_nvr_redownload'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S008': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_archive_worker_recovery.py::test_s008_cross_volume_orphan_is_adopted_without_duplicate_or_download'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S009': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_archive_queue.py::test_archive_claim_is_renewable_and_only_stale_claims_recover',
            'tests/test_archive_worker_recovery.py::test_s009_killed_download_recovers_quarantines_part_and_completes_once'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S010': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s010_digest_challenge_and_invalid_credentials_fail_closed'],
            cwd=ROOT/'implementation',timeout_seconds=90,expected_test_count=3),
        'S011': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_s011_nvr_faults.py::test_s011_search_503_recovers_after_stable_rescans_without_duplicate',
            'tests/test_s011_nvr_faults.py::test_s011_download_timeout_retries_once_without_duplicate_asset',
            'tests/test_s011_nvr_faults.py::test_s011_offline_search_peer_retries_with_online_peer_without_duplicate'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S012': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_reconciliation_storage.py::test_s012_reports_storage_ledger_mismatches_without_mutating_rows_or_files'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S013': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_archive_worker_recovery.py::test_s013_storage_faults_fail_closed_without_touching_siblings',
            'tests/test_archive_worker_recovery.py::test_s013_write_time_eacces_after_preflight_fails_closed'],
            cwd=ROOT/'implementation',timeout_seconds=90,expected_test_count=4),
        'S014': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_scan_nvr.py::test_single_camera_failure_does_not_block_stable_peer'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S015': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_vela_scan_mutation.py::ScanMutationTests::test_S015_actual_product_hash_is_order_independent',
            'tests/test_vela_scan_mutation.py::ScanMutationTests::test_S015_changed_source_is_not_stable',
            'tests/test_vela_scan_mutation.py::ScanMutationTests::test_S015_real_order_sensitive_mutant_is_killed'],
            cwd=ROOT/'implementation',timeout_seconds=30),
        'S016': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_scan_nvr.py::test_completed_history_is_not_requeried_when_auto_enqueue_is_disabled'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S017': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_two_workers_racing_one_source_create_one_verified_asset'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S018': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_archive_queue.py::test_sqlite_writer_lock_rolls_back_enqueue_and_retry_preserves_source',
            'tests/test_migrations.py::test_active_job_index_migration_preserves_canonical_rows',
            'tests/test_migrations.py::test_active_job_index_migration_refuses_duplicates_without_deleting_rows'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S019': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_media_derivation_integration.py::test_component_multicontainer_codec_matrix_stream_copy_and_pts'],
            cwd=ROOT/'implementation',timeout_seconds=240,expected_test_count=3),
        'S020': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_transcribe_cli.py::test_s020_asr_timeout_is_retryable_without_changing_media',
            'tests/test_transcribe_cli.py::test_s020_invalid_segment_is_rejected_and_media_stays_derived'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S021': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_transcribe_cli.py::test_s021_same_basename_wrapper_revision_is_persisted_as_transcript_provenance'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S022': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_frame_reduce_benchmark.py::test_s022_real_frame_reducer_selects_vfr_source_pts_and_motion_transitions'],
            cwd=ROOT/'implementation',timeout_seconds=240,expected_test_count=1),
        'S023': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_analysis_package.py::test_s023_published_artifact_snapshot_survives_transcript_retry'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S024': command_handler(['npx','playwright','test','--config=playwright.config.js','--grep','@S024'],
            cwd=ROOT/'implementation/frontend-r2',timeout_seconds=180,forward_stderr=True,
            expected_playwright_tag='@S024'),
        'S025': command_handler(['npx','playwright','test','--config=playwright.config.js','--grep','@S025'],
            cwd=ROOT/'implementation/frontend-r2',timeout_seconds=180,forward_stderr=True,
            expected_playwright_tag='@S025'),
        # S027 stays blocked as a complete catalog scenario: its secret
        # reflection component is real, but the RAW cleanup test currently
        # injects environment names the product CLI does not consume.
        'S031': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_vela_mutation_matrix.py::test_s031_current_day_enqueue_canary_rejects_cutoff_mutant',
            'tests/test_vela_mutation_matrix.py::test_s031_future_ending_video_canary_rejects_cutoff_mutant',
            'tests/test_vela_mutation_matrix.py::test_s031_incorrect_hash_canary_rejects_digest_mutant',
            'tests/test_vela_mutation_matrix.py::test_s031_duplicate_enqueue_canary_rejects_missing_active_job_guard'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        'S032': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_reconciliation_storage.py::test_s032_fifteen_day_history_drift_reconciliation_no_overwrite'],
            cwd=ROOT/'implementation',timeout_seconds=180),
        # These scenarios stay unregistered until every frozen subcase has
        # real injection and independent oracles. S026's process-local socket canary cannot constrain child egress;
        # S028/029/030 lack full system-level or multi-stage acceptance.
        # Component tests cannot turn broader scenarios green.
        'S033': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_vela_mutation_matrix.py::test_s033_http_200_error_xml_and_invalid_video_are_rejected'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S034': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_vela_mutation_matrix.py::test_s034_singapore_midnight_cutoff_and_nvr_floor_match_utc_oracle'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S035': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_media_derivation_integration.py::test_s035_ten_minute_derivation_and_large_audio_transcript_are_bounded'],
            cwd=ROOT/'implementation',timeout_seconds=240),
        'S036': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_mock_nvr_acceptance.py::test_s036_optional_stage_failures_preserve_verified_archive'],
            cwd=ROOT/'implementation',timeout_seconds=240),
    }


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('mode',choices=['scenario','once','continuous'])
    p.add_argument('--scenario-id')
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--source-sha',required=True)
    p.add_argument('--interval',type=int,default=900)
    a=p.parse_args(argv)
    if not SHA.fullmatch(a.source_sha):
        p.error('source-sha must be exact 40-character SHA')
    if a.mode=='scenario' and not a.scenario_id:
        p.error('scenario mode requires --scenario-id')
    if a.mode!='scenario' and a.scenario_id:
        p.error('--scenario-id is valid only in scenario mode')
    if a.mode=='continuous' and a.interval < 1:
        p.error('--interval must be positive')
    try:
        verify_source_revision(ROOT, a.source_sha)
    except ValueError as exc:
        p.error(str(exc))
    # Refuse a symlinked state root: all evidence and queue writes must stay
    # inside the caller-selected isolated UAT tree.
    state_path = a.state.absolute()
    if any(part.is_symlink() for part in (state_path, *state_path.parents)):
        p.error('--state must not be a symlink')
    if a.state.exists() and not a.state.is_dir():
        p.error('--state must be a directory')
    try:
        a.state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        p.error(f'cannot prepare --state directory: {exc.strerror or type(exc).__name__}')
    if any(item.is_symlink() for item in a.state.rglob('*')):
        p.error('--state tree must not contain symlinks')
    catalog_path = ROOT/'design/P16R2_VELA_SOAK_G0_SCENARIOS_v1.json'
    if not catalog_path.is_file() and ROOT.name == 'implementation':
        catalog_path = ROOT.parent/'design/P16R2_VELA_SOAK_G0_SCENARIOS_v1.json'
    if not catalog_path.is_file():
        p.error('scenario catalog is unavailable')
    catalog=json.loads(catalog_path.read_text())
    if a.scenario_id and a.scenario_id not in {s['id'] for s in catalog['scenarios']}:
        p.error('unknown scenario')
    store=EvidenceStore(a.state)
    # Recover queue from authoritative history after an interrupted enqueue.
    enqueue_failures(a.state,store.records())
    handlers=registry()
    if a.mode=='scenario':
        handlers={k:v for k,v in handlers.items() if k==a.scenario_id}
    def guard():
        resource_guard(a.state)
    def cycle():
        # Continuous mode must never tag code changed between cycles with an
        # older source revision.
        verify_source_revision(ROOT, a.source_sha)
        if a.mode == 'scenario':
            result=run_scenario(catalog,a.scenario_id,handlers,store,
                                source_sha=a.source_sha,guard=guard)
        else:
            result=run_cycle(catalog,handlers,store,source_sha=a.source_sha,guard=guard)
        enqueue_failures(a.state,result['results'])
        print(json.dumps(result),flush=True)
        return result
    if a.mode=='continuous':
        continuous(cycle,stop_requested=lambda:(a.state/'STOP').exists(),interval_seconds=a.interval)
        return 0
    result=cycle()
    if a.mode=='scenario':
        return 0 if next(r for r in result['results'] if r['scenario_id']==a.scenario_id)['status']=='PASS' else 1
    return 0 if result['complete'] else 1


if __name__=='__main__':
    raise SystemExit(main())
