"""Candidate executable registry and durable repair queue; never grants day credit."""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from vela_soak_runtime import EvidenceStore, command_handler, continuous, digest, run_cycle

ROOT = Path(__file__).resolve().parents[2]


def enqueue_failures(root, records):
    """Deduplicate same-version reproductions, retain every evidence reference."""
    with sqlite3.connect(Path(root) / 'repair-queue.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS tasks (key TEXT PRIMARY KEY, scenario TEXT, seed TEXT, source TEXT, status TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS observations (run_id TEXT PRIMARY KEY, task_key TEXT, evidence_hash TEXT)')
        for r in records:
            if r['status'] != 'FAIL':
                continue
            key = digest([r['scenario_id'], r['seed'], r['source_sha']])
            db.execute('INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,?)',
                       (key,r['scenario_id'],r['seed'],r['source_sha'],'NEEDS_REPRO_AND_FIX'))
            db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?)',
                       (r['run_id'],key,digest(r)))


def registry():
    # Each registered test contains product assertions; unimplemented scenarios
    # deliberately remain absent, never credited by generic regression results.
    return {
        'S014': command_handler([sys.executable,'-m','pytest','-q',
            'tests/test_scan_nvr.py::test_single_camera_failure_does_not_block_stable_peer'],
            cwd=ROOT/'implementation',timeout_seconds=90),
        'S015': command_handler([sys.executable,'-m','unittest','discover','-s','tests',
            '-p','test_vela_scan_mutation.py'],cwd=ROOT/'implementation',timeout_seconds=30),
    }


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('mode',choices=['scenario','once','continuous'])
    p.add_argument('--scenario-id')
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--source-sha',required=True)
    p.add_argument('--interval',type=int,default=900)
    a=p.parse_args(argv)
    if len(a.source_sha)!=40 or any(c not in '0123456789abcdef' for c in a.source_sha):
        p.error('source-sha must be exact 40-character SHA')
    if a.mode=='scenario' and not a.scenario_id:
        p.error('scenario mode requires --scenario-id')
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
        if shutil.disk_usage(a.state).free < 6*1024**3:
            raise OSError('HOST_FREE_BELOW_LIMIT')
    def cycle():
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
