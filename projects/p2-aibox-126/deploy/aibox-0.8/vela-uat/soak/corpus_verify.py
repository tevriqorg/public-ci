#!/usr/bin/env python3
"""Read-only real public corpus integrity check: preflight != live ingestion."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from soak_engine import append_event, failing_incident, now_utc

# Deliberate allowlist. No unbounded crawlers, no URL fetch, no private media.
SOURCE_FILENAME = {
    "rfc-editor:rfc9110:text": "rfc9110.txt",
    "rfc-editor:rfc9111:pdf": "rfc9111.pdf",
    "nasa:s134e010062:photo": "nasa-iss-horizon.jpg",
    "nasa:artemis-i-launch:mobile-video": "nasa-artemis-i-mobile.mp4",
    "nasa:artemis-i-launch:20s-clip": "nasa-artemis-i-20s-clip.mp4",
}


def inspect_corpus(corpus_root: Path, manifest_path: Path):
    if not corpus_root.is_dir() or not manifest_path.is_file():
        raise ValueError("approved source directory or oracle manifest missing")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    items=manifest.get("items",[])
    expected={x["source_id"]: x for x in items}
    if not set(SOURCE_FILENAME) <= set(expected):
        raise ValueError("missing required frozen real source metadata")
    records=[]
    for source_id,filename in SOURCE_FILENAME.items():
        item=expected[source_id]
        full=corpus_root/filename
        if full.is_symlink() or not full.is_file() or full.resolve().parent!=corpus_root.resolve():
            raise ValueError("real source unavailable or escapes private corpus root")
        if full.stat().st_size!=item["size_bytes"]:
            raise ValueError("real source size differs from frozen record")
        h=hashlib.sha256()
        with full.open("rb") as stream:
            while block:=stream.read(1024*1024):
                h.update(block)
        if h.hexdigest()!=item["sha256"]:
            raise ValueError("real source SHA differs from frozen record")
        records.append({"source_id":source_id,"sha256":h.hexdigest(),"bytes":full.stat().st_size})
    return {"result":"PRECHECK_PASS","case_id":"AR01","mode":"preflight",
            "timestamp_utc":now_utc(),"source_count":len(records),
            "source_sha_digest":hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest()}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state-dir",type=Path,required=True)
    p.add_argument("--corpus-root",type=Path,required=True)
    p.add_argument("--manifest",type=Path,required=True)
    p.add_argument("--notify",action="store_true",help="On failure use validated GitHub issue alert")
    args=p.parse_args()
    try:
        result=inspect_corpus(args.corpus_root,args.manifest)
    except (OSError,KeyError,ValueError,TypeError,json.JSONDecodeError) as e:
        result={"result":"PRECHECK_FAIL","case_id":"AR01","mode":"preflight",
                "timestamp_utc":now_utc(),"error_type":type(e).__name__}
        # Only registered root, not the raw filenames or private bytes, in incident.
        incident=failing_incident(args.state_dir,case_id="AR01",signature="raw_corruption",source_id="public-corpus")
        if args.notify:
            from watchdog import notify_issue_once
            result["notification"]=notify_issue_once(args.state_dir,incident["incident_id"])
    append_event(args.state_dir/"corpus_checks.jsonl",result)
    print(json.dumps(result,sort_keys=True))
    if result["result"]!="PRECHECK_PASS":
        sys.exit(2)


if __name__=="__main__":
    main()
