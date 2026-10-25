"""#126: expiring clone session must not silently break persistent L2."""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from issue126_canary_session_renew import ensure_fresh_session


def jwt(kind, exp, *, tenant="canary-tenant", user="canary-user"):
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = {"type": kind, "exp": exp, "user_id": user}
    if kind == "access":
        payload["tenant_id"] = tenant
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.signature"


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, n):
        assert len(self.data) <= n
        return self.data


class FakeOpener:
    def __init__(self, access, refresh, *, bad_tenant=False, status=200):
        self.calls = []
        self.access = access
        self.refresh = refresh
        self.status = status

    def open(self, request, timeout):
        self.calls.append((request.full_url, request.get_method()))
        assert request.full_url.startswith("http://127.0.0.1:18082/api/v1/")
        if request.full_url.endswith("/auth/refresh"):
            assert request.get_method() == "POST"
            assert "refreshToken" in json.loads(request.data)
            return FakeResponse(self.status, {
                "success": True, "access_token": self.access,
                "refresh_token": self.refresh,
            })
        assert "/datasource?kb_id=" in request.full_url
        assert request.get_method() == "GET"
        return FakeResponse(200, [])


class SessionRenewContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        (self.root / "test-kb.json").write_text('{"kb_id":"canary-kb"}')
        self.now = 1800000000
        self.old_access = jwt("access", self.now + 120)
        self.old_refresh = jwt("refresh", self.now + 600000)
        self.path = self.root / "session.json"
        self.write_session(self.old_access, self.old_refresh)

    def write_session(self, access, refresh):
        self.path.write_text(json.dumps({"token": access, "refresh_token": refresh, "user": {"id": "canary-user"}}))
        os.chmod(self.path, 0o600)

    def invoke(self, opener, now=None):
        return ensure_fresh_session(self.root, guard=lambda _: {"clone_db": "aibox126_canary"},
                                    opener=opener, now=self.now if now is None else now)

    def test_live_numeric_tenant_claim_refreshes(self):
        self.write_session(jwt("access", self.now+120,tenant=10000),self.old_refresh)
        result=self.invoke(FakeOpener(jwt("access",self.now+86400,tenant=10000),jwt("refresh",self.now+600000)))
        self.assertTrue(result["rotated"])

    def test_refresh_expiring_access_and_preserve_private_atomic_file(self):
        new_access = jwt("access", self.now + 86400)
        new_refresh = jwt("refresh", self.now + 600000)
        opener = FakeOpener(new_access, new_refresh)
        result = self.invoke(opener)
        self.assertTrue(result["rotated"])
        self.assertEqual(result["result"], "PASS_CANARY_SESSION_FRESH_ONLY")
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(json.loads(self.path.read_text())["token"], new_access)
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], new_refresh)
        self.assertEqual(json.loads(self.path.read_text())["user"]["id"], "canary-user")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(new_access, json.dumps(result))
        self.assertNotIn(new_refresh, json.dumps(result))

    def test_fresh_access_skips_refresh(self):
        self.write_session(jwt("access", self.now + 86400), self.old_refresh)
        opener = FakeOpener("", "")
        result = self.invoke(opener)
        self.assertFalse(result["rotated"])
        self.assertEqual(opener.calls, [])

    def test_mismatched_tenant_fails_without_overwriting(self):
        opener = FakeOpener(jwt("access", self.now + 86400, tenant="other-tenant"),
                            jwt("refresh", self.now + 600000))
        with self.assertRaises(ValueError):
            self.invoke(opener)
        self.assertEqual(json.loads(self.path.read_text())["token"], self.old_access)

    def test_redirect_status_fails_closed(self):
        opener = FakeOpener(jwt("access", self.now + 86400), jwt("refresh", self.now + 600000),
                            status=302)
        with self.assertRaises(ValueError):
            self.invoke(opener)
        self.assertEqual(json.loads(self.path.read_text())["token"], self.old_access)

    def test_expired_refresh_fails_without_network(self):
        self.write_session(self.old_access, jwt("refresh", self.now - 1))
        opener = FakeOpener("", "")
        with self.assertRaises(ValueError):
            self.invoke(opener)
        self.assertEqual(opener.calls, [])

    def test_hardlinked_session_rejected(self):
        os.link(self.path, self.root / "shadow-session.json")
        with self.assertRaises(ValueError):
            self.invoke(FakeOpener("", ""))

    def test_refresh_saved_even_if_followup_probe_fails(self):
        new_access = jwt("access", self.now + 86400)
        new_refresh = jwt("refresh", self.now + 600000)
        class ProbeFailure(FakeOpener):
            def open(self, request, timeout):
                if "/datasource?" in request.full_url:
                    return FakeResponse(503, {})
                return super().open(request, timeout)
        with self.assertRaises(ValueError):
            self.invoke(ProbeFailure(new_access, new_refresh))
        self.assertEqual(json.loads(self.path.read_text())["token"], new_access)
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], new_refresh)

    def test_symlink_session_rejected(self):
        other = self.root / "other.json"
        other.write_text(self.path.read_text())
        self.path.unlink()
        self.path.symlink_to(other)
        with self.assertRaises(ValueError):
            self.invoke(FakeOpener("", ""))


if __name__ == "__main__":
    unittest.main()


class DailySessionIntegration(unittest.TestCase):
    """The four frozen L2 business probes must be gated by clone auth renewal."""

    def setUp(self):
        from unittest.mock import patch
        self.patch = patch
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.source = self.root / "source"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "SOURCE_REVISION").write_text("test-only")
        from issue126_canary_daily import CASES
        for _, name, _ in CASES:
            (self.source / "scripts" / name).write_text("# test-only")

    def test_expired_session_blocks_before_any_business_write(self):
        from issue126_canary_daily import run
        with self.patch("issue126_canary_daily.verify_live_canary",
                        return_value={"source_sha": "test"}), self.patch(
                        "issue126_canary_daily.ensure_fresh_session",
                        side_effect=ValueError("expired refresh")), self.patch(
                        "issue126_canary_daily.subprocess.run") as subprocess:
            with self.assertRaises(ValueError):
                run(self.root, source_root=self.source)
            subprocess.assert_not_called()
        events = [json.loads(x) for x in (self.root / "l2-preflight-results.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["result"], "PRECHECK_BLOCKED_L2_SUBSET")
        self.assertIsNone(events[-1]["trial_id"])
        self.assertFalse(events[-1]["formal_soak_started"])
        self.assertEqual(events[-1]["checks"], [])

    def test_session_probe_runs_before_four_business_checks(self):
        from unittest.mock import Mock
        from issue126_canary_daily import run
        events = []
        payloads = [
            {"result": "PASS_CANARY_AT_REST_ONLY", "replays": 3,
             "revision_count": 1, "raw_bytes_copied": 0, "formal_soak_started": False},
            {"result": "PASS_CANARY_TRANSIT_HANDOFF_ONLY", "state": "complete",
             "staging_cleaned": True, "formal_soak_started": False},
            {"result": "PASS_CANARY_CONTINUOUS_BACKFILL_REPLAY_ONLY",
             "cursor_persisted": True, "real_search_hit": True, "formal_soak_started": False},
            {"result": "PRECHECK_PASS_CLONE_KB_SCOPE_ONLY", "case_count": 10,
             "formal_soak_started": False},
        ]
        def renew(*_args, **_kwargs):
            events.append("renew")
            return {"rotated": True, "access_expires_at_utc": "2026-10-25T00:00:00Z"}
        def execute(*_args, **_kwargs):
            events.append("business")
            return Mock(returncode=0, stdout=json.dumps(payloads.pop(0)))
        with self.patch("issue126_canary_daily.verify_live_canary",
                        return_value={"source_sha": "test"}), self.patch(
                        "issue126_canary_daily.ensure_fresh_session", side_effect=renew), self.patch(
                        "issue126_canary_daily.subprocess.run", side_effect=execute):
            self.assertEqual(run(self.root, source_root=self.source), 0)
        self.assertEqual(events, ["renew", "business", "business", "business", "business"])
        final = json.loads((self.root / "l2-preflight-results.jsonl").read_text().splitlines()[-1])
        self.assertTrue(final["session_renewed"])
        self.assertEqual(final["result"], "PRECHECK_PASS_L2_SUBSET")
        self.assertIsNone(final["trial_id"])
