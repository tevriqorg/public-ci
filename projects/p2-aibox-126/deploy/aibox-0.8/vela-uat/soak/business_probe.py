#!/usr/bin/env python3
"""Read-only authenticated hourly knowledge probe against existing WeKnora.

Requires a separately provisioned scoped read key file and a frozen, dedicated
test Knowledge ID/expected phrases. No ingestion, deletion or secret logging.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

from gate_guard import validate_gate
from soak_engine import append_event, failing_incident, now_utc, _verified_oracle_sources

# Only the dedicated #126 canary API. Never query the original owner UAT.
API_URL = "http://127.0.0.1:18082/api/v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # urllib normally forwards request headers, including X-API-Key.
        return None



def secure_read(path: Path):
    if path.is_symlink() or not path.is_file() or (path.stat().st_mode & 0o077):
        raise ValueError("token path missing or readable by others")
    value = path.read_text(encoding="utf-8").strip()
    if len(value)<16 or "\n" in value:
        raise ValueError("invalid scoped read key")
    return value


def api_get(resource: str, token: str, transport=None):
    if not resource.startswith("/") or ".." in resource or "//" in resource or "?" in resource:
        raise ValueError("unsafe resource path")
    url = API_URL + resource
    if transport:
        return transport(url, token)
    req = urllib.request.Request(url, headers={"X-API-Key": token,
                                                "User-Agent": "aibox-p2-readonly-soak-v1"})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=12) as response:
        if response.status != 200:
            raise ValueError("knowledge API did not return 200")
        raw = response.read(2_000_001)
    if len(raw)>2_000_000:
        raise ValueError("response exceeds read-only probe cap")
    return json.loads(raw)


def _snippets(obj):
    """Find textual proof in returned chunks, not in our own reference oracle."""
    if isinstance(obj, dict):
        for k,v in obj.items():
            if k in {"content","text","markdown","page_content"} and isinstance(v,str):
                yield v
            elif isinstance(v,(dict,list)):
                yield from _snippets(v)
    elif isinstance(obj,list):
        for x in obj:
            yield from _snippets(x)


def knowledge_case(profile, token, transport=None):
    """Fail closed unless KB, knowledge ID, phrase AND source locator are real."""
    kb_id = profile.get("kb_id")
    entries = profile.get("knowledge", [])
    if not isinstance(entries,list) or not entries or not kb_id:
        raise ValueError("missing frozen test KB/knowledge manifest")
    if len(entries)>4:
        raise ValueError("hourly probe is bounded to at most four documents")
    results=[]
    for row in entries:
        knowledge_id=row["knowledge_id"]
        # Only UUID identifiers: no untrusted paths or query building.
        import uuid
        uuid.UUID(kb_id);uuid.UUID(knowledge_id)
        info = api_get("/knowledge/"+knowledge_id,token,transport)
        chunks = api_get("/chunks/"+knowledge_id,token,transport)
        if info.get("success") is not True or chunks.get("success") is not True:
            raise ValueError("knowledge API failure envelope")
        datum = info.get("data")
        if not isinstance(datum,dict) or datum.get("id") != knowledge_id or datum.get("knowledge_base_id") != kb_id:
            raise ValueError("wrong knowledge ID or wrong test KB")
        parts = list(_snippets(chunks.get("data")))
        if not parts:
            raise ValueError("no indexed chunk content")
        phrase = row.get("expected_phrase","")
        if not phrase or not any(phrase in p for p in parts):
            raise ValueError("expected phrase not retrieved from indexed chunks")
        # This is a read-only content regression; true citations are separate KB02/03 cases.
        results.append({"knowledge_id":knowledge_id,"chunk_count":len(parts),
                        "phrase_evidence_sha256":hashlib.sha256(phrase.encode()).hexdigest()})
    return results


def run(state_dir:Path,profile_path:Path,token_file:Path,transport=None,preflight=False,notify=False):
    gate, reason = validate_gate(state_dir/"gate.json")
    if preflight:
        # An explicitly preflight-only timer must NEVER emit a formal PASS,
        # even after a future independent Gate is installed.
        gate = None
    if not gate and not preflight:
        return {"result":"BLOCKED","reason":reason,"mode":"preflight"}
    if profile_path.is_symlink() or not profile_path.is_file() or (profile_path.stat().st_mode & 0o077):
        return {"result":"BLOCKED","reason":"private test profile missing/insecure"}
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        # Frozen oracle referenced by signed gate, not replaceable by model output.
        digest=hashlib.sha256(profile_path.read_bytes()).hexdigest()
        if gate:
            # The signed oracle hash identifies frozen-oracle.json, NOT the
            # separate L1 read-only profile. The manifest binds its profile hash.
            if not _verified_oracle_sources(state_dir, gate):
                raise ValueError("signed frozen source manifest missing or invalid")
            manifest = json.loads((state_dir / "frozen-oracle.json").read_bytes())
            if manifest.get("l1_profile_sha256") != digest:
                raise ValueError("L1 profile not bound to signed frozen manifest")
        token=secure_read(token_file)
        rows=knowledge_case(profile,token,transport)
        result={"case_id":"KB01","result":"PRECHECK_PASS" if not gate else "PASS",
                "mode":"preflight" if not gate else "soak",
                "trial_id":gate["trial_id"] if gate else None,
                "oracle_sha256":gate["oracle_sha256"] if gate else digest,"timestamp_utc":now_utc(),
                "knowledge_count":len(rows),"evidence_sha256":hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()}
        append_event(state_dir/"business.jsonl",result)
        return result
    except (OSError,KeyError,TypeError,ValueError,urllib.error.URLError) as e:
        # Do not log the network URL, returned payload or secrets.
        category=("locator_mismatch" if "knowledge" in str(e) or "phrase" in str(e)
                  else "monitor_blindness")
        result={"case_id":"KB01","result":"PRECHECK_FAIL" if not gate else "FAIL",
                "mode":"preflight" if not gate else "soak",
                "trial_id":gate["trial_id"] if gate else None,
                "oracle_sha256":gate["oracle_sha256"] if gate else (digest if "digest" in locals() else None),"timestamp_utc":now_utc(),
                "error_type":type(e).__name__,"error_category":category}
        append_event(state_dir/"business.jsonl",result)
        incident=failing_incident(state_dir,case_id="KB01",signature=category,source_id="hourly-probe")
        if notify:
            from watchdog import notify_issue_once
            result["notification"]=notify_issue_once(state_dir,incident["incident_id"])
        return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state-dir",type=Path,required=True)
    p.add_argument("--profile",type=Path,required=True)
    p.add_argument("--read-key",type=Path,required=True)
    p.add_argument("--preflight-verify",action="store_true",
                   help="Read-only smoke before gate; never counts as a soak PASS")
    p.add_argument("--notify",action="store_true",
                   help="On failed read-only probe, notify #126 with a sanitized incident")
    a=p.parse_args()
    result=run(a.state_dir,a.profile,a.read_key,preflight=a.preflight_verify,notify=a.notify)
    print(json.dumps(result,sort_keys=True))
    if result["result"] not in ("PASS","PRECHECK_PASS"):
        sys.exit(2)


if __name__=="__main__":
    main()
