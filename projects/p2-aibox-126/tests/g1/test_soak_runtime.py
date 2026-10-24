"""P2 #126 security and fault-learning regressions in disposable directories."""
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy/aibox-0.8/vela-uat/soak"))
from gate_guard import FROZEN_MATRIX_SHA256,canonical_payload,validate_gate
from soak_engine import (
    append_event,read_events,failing_incident,transition_incident,evaluate,load_rules
)
from watchdog import latest_sample_fresh,check
from business_probe import knowledge_case,run as business_run


class SoakRuntimeContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.key=self.root/"approval.key"
        self.key.write_bytes(os.urandom(40));self.key.chmod(0o600)
        self.start=dt.datetime(2026,10,20,tzinfo=dt.timezone.utc)
        self.trial="P2-126-example"
        self.gate={"schema":"p2-soak-gate-v1","trial_id":self.trial,
            "start_utc":self.start.isoformat(),"matrix_sha256":FROZEN_MATRIX_SHA256,
            "oracle_sha256":"a"*64,"p2_sha":"b"*40,"weknora_sha":"c"*40,
            "runtime_digest":"sha256:"+"d"*64,"rollback_snapshot":"snapshot-1",
            "approval_evidence_sha256":"e"*64,"at_rest_receipt_sha256":"1"*64,
            "transit_receipt_sha256":"2"*64,"continuous_receipt_sha256":"3"*64}

    def install_gate(self):
        gate=dict(self.gate)
        gate["signature_hmac_sha256"]=hmac.new(self.key.read_bytes(),canonical_payload(gate),hashlib.sha256).hexdigest()
        path=self.root/"gate.json";path.write_text(json.dumps(gate));path.chmod(0o600)
        return path

    def test_missing_or_old_bare_timestamp_cannot_start(self):
        self.assertIsNone(validate_gate(self.root/"gate.json")[0])
        p=self.root/"gate.json"
        p.write_text(json.dumps({"start_utc":self.start.isoformat()}))
        self.assertIsNone(validate_gate(p,self.key)[0])
        self.assertEqual(evaluate(self.root)["mode"],"preflight")

    def test_signed_gate_enforces_integrity(self):
        p=self.install_gate()
        good,reason=validate_gate(p,self.key,now=self.start+dt.timedelta(days=1))
        self.assertIsNotNone(good,reason)
        d=json.loads(p.read_text());d["transit_receipt_sha256"]="f"*64;p.write_text(json.dumps(d))
        self.assertIsNone(validate_gate(p,self.key)[0])

    def test_no_key_or_insecure_key_fails_closed(self):
        p=self.install_gate()
        self.assertIsNone(validate_gate(p)[0])
        self.key.chmod(0o644)
        self.assertIsNone(validate_gate(p,self.key)[0])

    def test_360_slots_is_not_sufficient(self):
        self.install_gate()
        self.assertEqual(evaluate(self.root,now=self.start+dt.timedelta(hours=360),gate_key=self.key)["verdict"],"BLOCKED")
        for hour in range(360):
            when=self.start+dt.timedelta(minutes=20,hours=hour)
            append_event(self.root/"samples.jsonl",{"sample_time_utc":when.isoformat(),
                        "mode":"soak","trial_id":self.trial,"all_healthy":True})
        v=evaluate(self.root,now=self.start+dt.timedelta(hours=360,minutes=1),gate_key=self.key)
        self.assertEqual(v["l0_slots"],360)
        self.assertEqual(v["l1_slots"],0)
        self.assertEqual(v["verdict"],"BLOCKED")

    def test_seeded_bad_sha_generates_targeted_plan_not_fix(self):
        fixture=self.root/"real_sample.bin"
        fixture.write_bytes(b"source contents")
        expected=hashlib.sha256(fixture.read_bytes()).hexdigest()
        fixture.write_bytes(b"silently tampered")
        self.assertNotEqual(expected,hashlib.sha256(fixture.read_bytes()).hexdigest())
        event=failing_incident(self.root,case_id="TR04",signature="raw_corruption",
                                 source_id="seed-mp4")
        self.assertEqual(event["state"],"DETECTED")
        self.assertIn("TR01",event["targeted_cases"])
        self.assertTrue(event["trial_must_requalify"])
        self.assertFalse(event["oracle_change_allowed"])
        self.assertEqual(len(read_events(self.root/"case_proposals.jsonl")),1)
        self.assertRaises(ValueError,transition_incident,self.root,event["incident_id"],"VERIFIED_CLOSED",
                          {"artifact_sha256":"f"*64,"evaluator":"reviewer"})
        step=transition_incident(self.root,event["incident_id"],"TRIAGED",
                       {"artifact_sha256":"f"*64,"evaluator":"reviewer"})
        self.assertEqual(step["severity"],"P0")
        self.assertEqual(len(read_events(self.root/"incidents.jsonl")),2)

    def test_incident_dedup_and_learn_does_not_rewrite_oracle(self):
        for _ in range(3):
            x=failing_incident(self.root,case_id="CO05",signature="continuous_cursor",source_id="public-feed")
        self.assertEqual(x["event_seq"],3)
        self.assertEqual(x["state"],"REOPENED")
        self.assertEqual(len(read_events(self.root/"case_proposals.jsonl")),1)
        self.assertFalse(x["oracle_change_allowed"])
        self.assertIn("CO02",x["targeted_cases"])

    def test_watchdog_detects_missing_hour_without_faking_pass(self):
        ok,reason=latest_sample_fresh(self.root,now=self.start)
        self.assertFalse(ok)
        result=check(self.root,now=self.start)
        self.assertEqual(result["result"],"FAIL")
        self.assertEqual(result["mode"],"preflight")
        append_event(self.root/"samples.jsonl",{
            "sample_time_utc":self.start.isoformat(),"all_healthy":True,"mode":"preflight"})
        ok,_=latest_sample_fresh(self.root,now=self.start+dt.timedelta(minutes=10))
        self.assertTrue(ok)
        ok,reason=latest_sample_fresh(self.root,now=self.start+dt.timedelta(hours=2))
        self.assertFalse(ok)
        self.assertEqual(reason,"stale_samples")

    def test_readonly_business_probe_requires_gate(self):
        result=business_run(self.root,self.root/"nonexistent.json",self.root/"key")
        self.assertEqual(result["result"],"BLOCKED")
        self.assertFalse((self.root/"business.jsonl").exists())

    def test_preflight_readonly_is_not_accidental_soak_pass(self):
        kb=str(uuid.uuid4()); kid=str(uuid.uuid4())
        profile={"kb_id":kb,"knowledge":[{"knowledge_id":kid,"expected_phrase":"RFC 9110"}]}
        private=self.root/"private-profile.json";private.write_text(json.dumps(profile));private.chmod(0o600)
        secret=self.root/"read-key";secret.write_text("fake-read-only-test-token-long");secret.chmod(0o600)
        def transport(url,token):
            if "/chunks/" in url:
                return {"success":True,"data":[{"text":"RFC 9110 HTTP Semantics"}]}
            return {"success":True,"data":{"id":kid,"knowledge_base_id":kb}}
        result=business_run(self.root,private,secret,transport=transport,preflight=True)
        self.assertEqual(result["result"],"PRECHECK_PASS")
        self.assertIsNone(result["trial_id"])
        self.assertEqual(evaluate(self.root)["mode"],"preflight")
        self.assertNotEqual(result["result"],"PASS")

    def test_http_readonly_probe_frozen_phrase(self):
        kb=str(uuid.uuid4()); kid=str(uuid.uuid4())
        profile={"kb_id":kb,"knowledge":[{"knowledge_id":kid,"expected_phrase":"Cache-Control"}]}
        def mock(url,token):
            self.assertEqual(token,"mock-token-at-least-16-characters")
            if "/knowledge/" in url:
                return {"success":True,"data":{"id":kid,"knowledge_base_id":kb}}
            if "/chunks/" in url:
                return {"success":True,"data":{"items":[{"content":"Cache-Control response header"}]}}
            raise ValueError("unrecognized GET")
        out=knowledge_case(profile,"mock-token-at-least-16-characters",mock)
        self.assertEqual(out[0]["knowledge_id"],kid)
        profile["knowledge"][0]["expected_phrase"]="fake fabricated phrase"
        self.assertRaises(ValueError,knowledge_case,profile,"mock-token-at-least-16-characters",mock)

    def test_preflight_or_failed_l1_must_not_fill_soak_hour(self):
        self.install_gate()
        when = self.start + dt.timedelta(minutes=20)
        for mode, verdict in (("preflight", "PASS"), ("soak", "FAIL"), (None, "PASS")):
            append_event(self.root / "business.jsonl", {
                "timestamp_utc": when.isoformat(), "mode": mode,
                "trial_id": self.trial, "case_id": "KB01",
                "result": verdict, "oracle_sha256": self.gate["oracle_sha256"]})
        result = evaluate(self.root, now=self.start + dt.timedelta(hours=1),
                          gate_key=self.key)
        self.assertEqual(result["l1_slots"], 0)

    def test_bare_soak_l1_pass_cannot_count_without_artifact(self):
        self.install_gate()
        append_event(self.root / "business.jsonl", {
            "timestamp_utc": (self.start + dt.timedelta(minutes=20)).isoformat(),
            "mode": "soak", "trial_id": self.trial, "case_id": "KB01",
            "result": "PASS", "oracle_sha256": self.gate["oracle_sha256"]})
        result = evaluate(self.root, now=self.start + dt.timedelta(hours=1),
                          gate_key=self.key)
        self.assertEqual(result["l1_slots"], 0)

    def test_signed_gate_l1_profile_bound_to_frozen_manifest(self):
        kb, kid = str(uuid.uuid4()), str(uuid.uuid4())
        profile = {"kb_id": kb, "knowledge": [
            {"knowledge_id": kid, "expected_phrase": "RFC 9110"}]}
        path = self.root / "l1-profile.json"
        path.write_text(json.dumps(profile))
        path.chmod(0o600)
        profile_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {"schema": "p2-126-frozen-oracle-v1",
                    "sources": [{"source_id": "public-rfc",
                                 "sha256": "f" * 64}],
                    "l1_profile_sha256": profile_sha}
        frozen = self.root / "frozen-oracle.json"
        frozen.write_text(json.dumps(manifest))
        frozen.chmod(0o600)
        self.gate["oracle_sha256"] = hashlib.sha256(frozen.read_bytes()).hexdigest()
        self.install_gate()
        keyfile = self.root / "read-key"
        keyfile.write_text("synthetic-scoped-read-key-for-tests")
        keyfile.chmod(0o600)
        def transport(url, token):
            if "/chunks/" in url:
                return {"success": True, "data": [{"text": "RFC 9110"}]}
            return {"success": True, "data": {
                "id": kid, "knowledge_base_id": kb}}
        with patch("business_probe.verify_formal_runtime", return_value=True), \
                patch.dict(os.environ, {"P2_SOAK_GATE_KEY_FILE": str(self.key)}):
            result = business_run(self.root, path, keyfile, transport=transport)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["oracle_sha256"], self.gate["oracle_sha256"])
        self.assertEqual(result["mode"], "soak")
        artifact = self.root / "l1-evidence" / (result["evidence_sha256"] + ".json")
        self.assertTrue(artifact.is_file())
        verdict = evaluate(self.root, now=dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1),
                           gate_key=self.key)
        self.assertEqual(verdict["l1_slots"], 1)
        artifact.write_bytes(artifact.read_bytes() + b" ")
        verdict = evaluate(self.root, now=dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1),
                           gate_key=self.key)
        self.assertEqual(verdict["l1_slots"], 0)
        profile["knowledge"][0]["expected_phrase"] = "tampered oracle phrase"
        path.write_text(json.dumps(profile))
        path.chmod(0o600)
        with patch("business_probe.verify_formal_runtime", return_value=True), \
                patch.dict(os.environ, {"P2_SOAK_GATE_KEY_FILE": str(self.key)}):
            tampered = business_run(self.root, path, keyfile, transport=transport)
        self.assertEqual(tampered["result"], "FAIL")
        self.assertEqual(tampered["mode"], "soak")

    def test_daily_future_dated_pass_cannot_preload_soak(self):
        self.install_gate()
        for day in range(15):
            when = self.start + dt.timedelta(days=day, minutes=10)
            append_event(self.root / "daily.jsonl", {
                "timestamp_utc": when.isoformat(), "mode": "soak",
                "trial_id": self.trial, "result": "PASS",
                "oracle_sha256": self.gate["oracle_sha256"],
                "formats": ["text", "pdf"], "replay_pass": True,
                "incremental_pass": True})
        now = self.start + dt.timedelta(hours=25)
        result = evaluate(self.root, now=now, gate_key=self.key)
        self.assertEqual(result["daily_days"], 2)

    def test_preflight_daily_cannot_count_even_with_matching_trial_id(self):
        self.install_gate()
        append_event(self.root / "daily.jsonl", {
            "timestamp_utc": (self.start + dt.timedelta(hours=1)).isoformat(),
            "mode": "preflight", "trial_id": self.trial, "result": "PASS",
            "oracle_sha256": self.gate["oracle_sha256"],
            "formats": ["text", "pdf"], "replay_pass": True,
            "incremental_pass": True})
        result = evaluate(self.root, now=self.start + dt.timedelta(hours=26),
                          gate_key=self.key)
        self.assertEqual(result["daily_days"], 0)

    def test_log_detects_truncated_or_corrupt_data(self):
        path=self.root/"samples.jsonl"
        append_event(path,{"a":1})
        with path.open("a") as f:f.write("{broken}\n")
        self.assertRaises(ValueError,read_events,path)


if __name__=="__main__":
    unittest.main()
