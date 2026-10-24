#!/usr/bin/env python3
"""Adaptively propose (never execute) next soak test cases from incident evidence.

Risk selection cannot replace the immutable baseline, rewrite the oracle or
promote an exploratory case. Clone-only tests never escape to live UAT.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from soak_engine import load_rules, read_events, append_event, now_utc

LAYERS = {"live":{"live","host_read_only"}, "clone":{"clone"}}


def propose(state:Path, *, target:str, limit:int=8, timestamp=None):
    if target not in LAYERS:
        raise ValueError("explicit authorized sandbox target required")
    if limit<1 or limit>12:
        raise ValueError("proposal budget is between 1 and 12")
    cases, rules=load_rules()
    history=read_events(state/"case_results.jsonl")
    by_case=Counter(e.get("case_id") for e in history if e.get("result")=="PASS")
    issues=read_events(state/"incidents.jsonl")
    incident_counts=Counter(e.get("case_id") for e in issues if e.get("state") in ("DETECTED","REOPENED"))
    touched=Counter()
    for issue in issues:
        if issue.get("state") not in ("DETECTED","REOPENED"):
            continue
        for name in issue.get("targeted_cases",[]):
            touched[name]+=1
    candidates=[]
    for case_id,case in cases.items():
        if case["layer"] not in LAYERS[target]:
            continue
        # Untested critical paths and repeated incidents take priority; no unchecked raw data.
        score=(100 if case["severity"]=="P0" else 50)
        score+=min(200,100*touched[case_id]+60*incident_counts[case_id])
        if not by_case[case_id]:
            score+=30
        else:
            score+=max(0,20-min(20,by_case[case_id]*5))
        candidates.append({"case_id":case_id,"layer":case["layer"],"severity":case["severity"],
                           "score":score,"why":"new_or_repeated_failures_and_coverage"})
    candidates.sort(key=lambda x:(-x["score"],x["case_id"]))
    return {"schema":"p2-126-adaptive-proposal-v1","created_utc":timestamp or now_utc(),
            "target":target,"cases":candidates[:limit],"source":"frozen_35_cases_and_append_only_incidents",
            "execution_authorized":False,"changes_frozen_baseline":False,
            "requires_runtime_or_clone_scope_check":True}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state-dir",type=Path,required=True)
    p.add_argument("--target",choices=sorted(LAYERS),required=True)
    p.add_argument("--limit",type=int,default=8)
    p.add_argument("--save-proposal",action="store_true",
                   help="Append only the safe proposal; never execute a test")
    args=p.parse_args()
    plan=propose(args.state_dir,target=args.target,limit=args.limit)
    if args.save_proposal:
        append_event(args.state_dir/"adaptive_case_proposals.jsonl",plan)
    print(json.dumps(plan,sort_keys=True))


if __name__=="__main__":
    main()
