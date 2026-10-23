#!/usr/bin/env python3
"""Machine-readable handoff inventory. Missing executable cases are BLOCKED."""
import argparse
import hashlib
import json
from pathlib import Path
from soak_engine import load_rules
from gate_guard import validate_gate

READY_COMPONENTS = {
    "L0_health_sampler":"READY",
    "L0_watchdog":"IMPLEMENTED_NEEDS_TIMER_ACCEPTANCE",
    "L1_readonly_business_probe":"IMPLEMENTED_NEEDS_SCOPED_KEY_AND_TEST_ORACLE",
    "L2_daily_real_ingress_runner":"BLOCKED_NOT_IMPLEMENTED",
    "L3_cloned_fault_injection":"BLOCKED_NOT_IMPLEMENTED",
    "continuous_public_feed_adapter":"BLOCKED_NOT_IMPLEMENTED",
    "incident_evidence_and_case_proposals":"IMPLEMENTED_NEEDS_FIELD_TRIAL",
    "durable_agent_repair_handoff":"BLOCKED_NEEDS_AUTHORIZED_AGENT",
    "github_alert_delivery":"IMPLEMENTED_NEEDS_DELIVERY_TEST",
    "independent_acceptance_oracles":"BLOCKED_AUDIO_VIDEO_FEED",
    "P2_WeKnora_live_handoff":"BLOCKED_WEKNORA_PR6_PENDING",
    "live_three_entry_e2e":"BLOCKED",
    "public_actions_snapshot":"BLOCKED_UNTIL_CI_VERIFIED",
}


def inspect(state_dir:Path):
    cases,_=load_rules()
    gate,reason=validate_gate(state_dir/"gate.json")
    return {
        "issue":"tevriqorg/P2-AIBOX#126",
        "gate0_ready":False,   # Deliberately fail-closed: no auto-promotion.
        "trial_running":bool(gate),
        "trial_gate_reason":reason,
        "baseline_case_count":len(cases),
        "baseline_matrix_sha256":hashlib.sha256((Path(__file__).parent/"cases.json").read_bytes()).hexdigest(),
        "components":dict(READY_COMPONENTS),
        "mandatory_cases_missing_live_proof":sorted(cases),
        "next_actions":[
            "Review public Actions test snapshot",
            "Finish PR6 derived/external resource API and reviewed deploy",
            "Implement live At-rest/Transit/Continuous adapters and daily probes",
            "Freeze independent image/video/audio/feed oracle",
            "Demonstrate authorized Bug->PR->CI->targeted+full regression",
            "Verify independent watchdog and delivered GitHub escalation",
            "Independently approve signed Gate 0 with exact receipts",
        ],
    }


def main():
    p=argparse.ArgumentParser();p.add_argument("--state-dir",type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(inspect(a.state_dir),sort_keys=True,indent=2))


if __name__=="__main__":
    main()
