"""Candidate executable registry and durable repair queue; never grants day credit."""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from vela_soak_runtime import EvidenceStore, command_handler, continuous, digest, run_cycle, run_scenario

ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"^[0-9a-f]{40}$")


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
    failures = []
    batch_run_ids = {}
    # Validate the complete batch before opening a write transaction. A malformed
    # record must not leave even a partially initialized repair queue behind.
    for r in records:
        _validate_failure(r)
        if r['status'] != 'FAIL':
            continue
        key = digest([r['scenario_id'], r['seed'], r['source_sha']])
        evidence_hash = digest(r)
        previous = batch_run_ids.setdefault(r['run_id'], (key, evidence_hash))
        if previous != (key, evidence_hash):
            raise ValueError('REPAIR_OBSERVATION_CONFLICT')
        failures.append((key, r, evidence_hash))

    with sqlite3.connect(Path(root) / 'repair-queue.sqlite3') as db:
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS tasks (key TEXT PRIMARY KEY, scenario TEXT, seed TEXT, source TEXT, status TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS observations (run_id TEXT PRIMARY KEY, task_key TEXT, evidence_hash TEXT)')
        for key, r, evidence_hash in failures:
            prior = db.execute('SELECT task_key, evidence_hash FROM observations WHERE run_id=?', (r['run_id'],)).fetchone()
            if prior and prior != (key, evidence_hash):
                raise ValueError('REPAIR_OBSERVATION_CONFLICT')
            db.execute('INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,?)',
                       (key, r['scenario_id'], r['seed'], r['source_sha'], 'NEEDS_REPRO_AND_FIX'))
            db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?)',
                       (r['run_id'], key, evidence_hash))


def registry():
    # Each registered test contains product assertions; unimplemented scenarios
    # deliberately remain absent, never credited by generic regression results.
    return {
        'S014': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_scan_nvr.py::test_single_camera_failure_does_not_block_stable_peer'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S015': command_handler([sys.executable,'-m','unittest','discover','-s','tests',
            '-p','test_vela_scan_mutation.py'],cwd=ROOT/'implementation',timeout_seconds=30),
        'S016': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_scan_nvr.py::test_completed_history_is_not_requeried_when_auto_enqueue_is_disabled'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S020': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_transcribe_cli.py::test_transcription_retry_records_retryable_failure_without_failing_media',
            'tests/test_transcribe_cli.py::test_invalid_asr_json_is_retryable_without_changing_derived_audio'],
            cwd=ROOT/'implementation',timeout_seconds=90),
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
    catalog=json.loads((ROOT/'design/P16R2_VELA_SOAK_G0_SCENARIOS_v1.json').read_text())
    if a.scenario_id and a.scenario_id not in {s['id'] for s in catalog['scenarios']}:
        p.error('unknown scenario')
    store=EvidenceStore(a.state)
    # Recover queue from authoritative history after an interrupted enqueue.
    enqueue_failures(a.state,store.records())
    handlers=registry()
    if a.mode=='scenario':
        handlers={k:v for k,v in handlers.items() if k==a.scenario_id}
    def guard():
        import shutil
        size=0
        for item in a.state.rglob('*'):
            if item.is_symlink():
                raise OSError('EVIDENCE_SYMLINK_FORBIDDEN')
            if item.is_file():
                size += item.stat().st_size
        if size > 64*1024**2:
            raise OSError('EVIDENCE_BUDGET_EXHAUSTED')
        if shutil.disk_usage(a.state).free < 6*1024**3:
            raise OSError('HOST_FREE_BELOW_LIMIT')
    def cycle():
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
