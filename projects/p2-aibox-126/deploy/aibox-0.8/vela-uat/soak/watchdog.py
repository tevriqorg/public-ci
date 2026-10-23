#!/usr/bin/env python3
"""Independent missed-slot detector; never treat non-delivery as sent alert."""
import argparse
import datetime as dt
import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys

from soak_engine import read_events, failing_incident, append_event, now_utc, evaluate


def latest_sample_fresh(state_dir, now=None, max_age_minutes=100):
    now=now or dt.datetime.now(dt.timezone.utc)
    snapshots=read_events(state_dir/"samples.jsonl")
    if not snapshots:
        return False,"missing_samples"
    try:
        t=dt.datetime.fromisoformat(snapshots[-1]["sample_time_utc"].replace("Z","+00:00"))
        age=(now-t).total_seconds()/60
        if t.tzinfo is None or age < -5 or age > max_age_minutes:
            return False,"stale_samples"
        if not snapshots[-1].get("all_healthy"):
            return False,"unhealthy_samples"
        return True,"fresh"
    except (KeyError,TypeError,ValueError):
        return False,"unreadable_samples"


def check(state_dir,now=None):
    ok,reason=latest_sample_fresh(state_dir,now)
    trial=evaluate(state_dir,now=now)
    if not ok:
        incident=failing_incident(state_dir,case_id="OB02",signature="monitor_blindness",source_id="l0-sampler")
        return {"result":"FAIL","reason":reason,"incident_id":incident["incident_id"],
                "mode":trial["mode"]}
    if trial["mode"]=="soak" and (trial.get("missing_l0_slots") or trial.get("missing_l1_slots")):
        incident=failing_incident(state_dir,case_id="OB02",signature="monitor_blindness",source_id="soak-coverage")
        return {"result":"FAIL","reason":"business_or_health_slot_missing",
                "incident_id":incident["incident_id"],"mode":"soak"}
    return {"result":"PASS","mode":trial["mode"],"reason":"read_only_watchdog_no_gate_promotion"}


def notify_issue_once(state_dir, incident_id, *, test_mode=False):
    """Notify once per incident episode, including a verified-closed recurrence.

    An episode changes only after VERIFIED_CLOSED followed by a new failure.
    Repeated watchdog polls of an open incident never produce duplicate alerts.
    Old delivery rows without episode are interpreted as episode zero.
    """
    state_dir = Path(state_dir)
    # Lock across deduplication and GitHub transport: simultaneous watchdogs
    # must not both report the same recurrence.
    lock_fd = os.open(state_dir / "delivery.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        history = [e for e in read_events(state_dir / "incidents.jsonl")
                   if e.get("incident_id") == incident_id]
        if not history:
            return "UNKNOWN_INCIDENT"
        if history[-1].get("state") == "VERIFIED_CLOSED":
            return "INCIDENT_CLOSED"
        episode = sum(e.get("state") == "VERIFIED_CLOSED" for e in history)
        already = any(e.get("status") == "CONFIRMED" and
                      e.get("incident_id") == incident_id and
                      e.get("episode", 0) == episode
                      for e in read_events(state_dir / "delivery.jsonl"))
        if already:
            return "ALREADY_CONFIRMED"
        if test_mode:
            message = (f"[DRILL — NO REAL INCIDENT] P2 #126 watchdog GitHub notification test: {incident_id} "
                       f"episode={episode}. Do not change long-test gate from this delivery test.")
        else:
            message = (f"P2 #126 watchdog INCIDENT {incident_id} episode={episode}: testing signal missing. "
                       "See protected Vela evidence. No success verdict; investigate.")
        run = subprocess.run(["gh", "issue", "comment", "126", "-R", "tevriqorg/P2-AIBOX",
                              "--body-file", "-"], input=message, capture_output=True,
                             text=True, timeout=15, check=False)
        record = {"incident_id": incident_id, "episode": episode,
                  "incident_event_seq": history[-1].get("event_seq"), "at_utc": now_utc()}
        if run.returncode != 0:
            append_event(state_dir / "delivery.jsonl", dict(record, status="FAILED",
                         reason="GitHub comment transport failed"))
            return "NOT_DELIVERED"
        append_event(state_dir / "delivery.jsonl", dict(record, status="CONFIRMED"))
        return "CONFIRMED"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main():
    p=argparse.ArgumentParser();p.add_argument("--state-dir",type=Path,required=True)
    p.add_argument("--notify",action="store_true",
                   help="Explicitly post sanitized incident to GitHub if authenticated")
    args=p.parse_args()
    status=check(args.state_dir)
    if status["result"]=="FAIL" and args.notify:
        status["delivery"]=notify_issue_once(args.state_dir,status["incident_id"])
    print(json.dumps(status,sort_keys=True))
    sys.exit(0 if status["result"]=="PASS" else 2)


if __name__=="__main__":
    main()
