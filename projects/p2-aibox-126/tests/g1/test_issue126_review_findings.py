"""Independent P2 #127 review red tests; no Vela writes or credentials."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy/aibox-0.8/vela-uat/soak"))
sys.path.insert(0,str(ROOT/"scripts"))
from soak_engine import append_event,evaluate,failing_incident,transition_incident
from test_soak_runtime import SoakRuntimeContract


class ReviewSecurityRegression(unittest.TestCase):
    def test_forged_case_evidence_must_not_count(self):
        obj=SoakRuntimeContract("test_signed_gate_enforces_integrity")
        obj.setUp();self.addCleanup(obj.doCleanups)
        obj.install_gate()
        append_event(obj.root/"case_results.jsonl",{
            "trial_id":obj.trial,"case_id":"AR01","result":"PASS",
            "oracle_sha256":obj.gate["oracle_sha256"],"evidence_sha256":"x"})
        verdict=evaluate(obj.root,now=obj.start+dt.timedelta(hours=360),
                         gate_key=obj.key)
        self.assertIn("AR01",verdict["missing_case_ids"],
                      "missing source SHA, layer, version and digest must never count")

    def test_closed_p0_invalidates_same_trial(self):
        obj=SoakRuntimeContract("test_signed_gate_enforces_integrity")
        obj.setUp();self.addCleanup(obj.doCleanups)
        now=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=5)
        obj.start=now-dt.timedelta(hours=360)
        obj.gate["start_utc"]=obj.start.isoformat()
        obj.install_gate()
        incident=failing_incident(obj.root,case_id="TR04",
                  signature="raw_corruption",source_id="late-soak-p0")
        for state in ("TRIAGED","REPRODUCED","FIX_IN_PR","CI_VERIFIED",
                      "CANARY_DEPLOYED","TARGETED_PASS","REGRESSION_PASS",
                      "LEARNING_COMMITTED","VERIFIED_CLOSED"):
            transition_incident(obj.root,incident["incident_id"],state,
                                {"artifact_sha256":"f"*64,"evaluator":"independent"})
        verdict=evaluate(obj.root,now=now,gate_key=obj.key)
        self.assertIn(incident["incident_id"],verdict["trial_invalidated_by"],
                      "closed critical incident still requires a fresh signed trial")

    def test_canary_dataplane_cannot_point_to_old_db(self):
        from p2_canary_guard import verify_canary_runtime
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/"storage").mkdir()
            state={"clone_db":"aibox126_canary_test","original_db":"weknora"}
            env={"DB_NAME":state["clone_db"],"REDIS_DB":"14",
                 "REDIS_ADDR":"127.0.0.1:56379",
                 "REDIS_PREFIX":"aibox126-canary",
                 "WEKNORA_REDIS_NAMESPACE":"aibox126-canary",
                 "LOCAL_STORAGE_BASE_DIR":str(root/"storage"),
                 "AIBOX_INGRESS_ALLOWED_RESOURCE_PREFIX":"local://10000/aibox126/"}
            def runtime(db):
                return dict(env,DB_NAME=db,WEKNORA_RUNTIME_ROLE="api")
            self.assertRaises(ValueError,verify_canary_runtime,root,state,env,
                              {"api":runtime("weknora"),"worker":dict(env,WEKNORA_RUNTIME_ROLE="worker",WEKNORA_WORKER_POOLS="core")})
            self.assertRaises(ValueError,verify_canary_runtime,root,state,env,
                              {"api":dict(runtime(state["clone_db"]),LOCAL_STORAGE_BASE_DIR="/tmp/shared"),
                               "worker":dict(env,WEKNORA_RUNTIME_ROLE="worker",WEKNORA_WORKER_POOLS="core")})

    def test_private_session_permissions_fail_closed(self):
        from p2_canary_guard import secure_canary_read
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);root.chmod(0o700)
            token=root/"session.json";token.write_text('{"token":"test"}')
            token.chmod(0o644)
            self.assertRaises(ValueError,secure_canary_read,root,token)
            token.chmod(0o600)
            self.assertEqual(secure_canary_read(root,token),token.read_text())
            root.chmod(0o755)
            self.assertRaises(ValueError,secure_canary_read,root,token)


    def test_watchdog_reopen_after_close_must_notify_again(self):
        from unittest.mock import patch
        from watchdog import notify_issue_once
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            incident=failing_incident(root,case_id="OB02",signature="monitor_blindness",source_id="l0-sampler")
            class OK:
                returncode=0
            with patch("watchdog.subprocess.run",return_value=OK()) as transport:
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"CONFIRMED")
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"ALREADY_CONFIRMED")
                for state in ("TRIAGED","REPRODUCED","FIX_IN_PR","CI_VERIFIED",
                              "CANARY_DEPLOYED","TARGETED_PASS","REGRESSION_PASS",
                              "LEARNING_COMMITTED","VERIFIED_CLOSED"):
                    transition_incident(root,incident["incident_id"],state,
                        {"artifact_sha256":"f"*64,"evaluator":"independent"})
                reopened=failing_incident(root,case_id="OB02",signature="monitor_blindness",source_id="l0-sampler")
                self.assertEqual(reopened["state"],"REOPENED")
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"CONFIRMED",
                    "a new episode after verified closure must alert again")
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"ALREADY_CONFIRMED")
                self.assertEqual(transport.call_count,2)


    def test_watchdog_failed_delivery_retries(self):
        from unittest.mock import patch
        from watchdog import notify_issue_once
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            incident=failing_incident(root,case_id="OB02",signature="monitor_blindness",source_id="soak-coverage")
            class Failed: returncode=1
            class OK: returncode=0
            with patch("watchdog.subprocess.run",side_effect=[Failed(),OK()]) as transport:
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"NOT_DELIVERED")
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"CONFIRMED")
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"ALREADY_CONFIRMED")
                self.assertEqual(transport.call_count,2)

    def test_watchdog_legacy_confirmation_dedup_only_first_episode(self):
        from unittest.mock import patch
        from watchdog import notify_issue_once
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            incident=failing_incident(root,case_id="OB02",signature="monitor_blindness",source_id="l0-sampler")
            append_event(root/"delivery.jsonl",{"incident_id":incident["incident_id"],"status":"CONFIRMED"})
            class OK: returncode=0
            with patch("watchdog.subprocess.run",return_value=OK()) as transport:
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"ALREADY_CONFIRMED")
                for state in ("TRIAGED","REPRODUCED","FIX_IN_PR","CI_VERIFIED",
                              "CANARY_DEPLOYED","TARGETED_PASS","REGRESSION_PASS",
                              "LEARNING_COMMITTED","VERIFIED_CLOSED"):
                    transition_incident(root,incident["incident_id"],state,
                        {"artifact_sha256":"f"*64,"evaluator":"independent"})
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"INCIDENT_CLOSED")
                failing_incident(root,case_id="OB02",signature="monitor_blindness",source_id="l0-sampler")
                self.assertEqual(notify_issue_once(root,incident["incident_id"]),"CONFIRMED")
                self.assertEqual(transport.call_count,1)


if __name__=="__main__":unittest.main()
