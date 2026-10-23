"""#126 regression: a stale/forged datasource marker must not redirect an admin
RSS sync to another knowledge base or another feed (clone-only)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import issue126_canary_continuous as target

URL = "https://github.com/tailscale/tailscale/releases.atom"
NAME = "P2-126 Tailscale public Atom canary"


class ContinuousMarkerBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.kb = str(uuid.uuid4())
        self.ident = str(uuid.uuid4())
        self.marker = self.root / "continuous-datasource.json"
        self.marker.write_text(json.dumps({"id": self.ident, "source_url": URL,
                                            "knowledge_base_id": self.kb}))
        self.marker.chmod(0o600)

    def remote(self):
        return {"id": self.ident, "knowledge_base_id": self.kb, "type": "rss",
                "name": NAME, "sync_mode": "incremental",
                "sync_deletions": False, "status": "paused",
                "config": {"type": "rss", "settings": {"feed_urls": URL}}}

    def load(self, remote=None):
        return target.load_bound_datasource(
            self.root, self.marker, self.kb, URL, lambda _route: remote or self.remote())

    def test_verified_existing_marker(self):
        self.assertEqual(self.load()["id"], self.ident)

    def test_marker_cannot_point_at_foreign_kb(self):
        d = self.remote()
        d["knowledge_base_id"] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            self.load(d)

    def test_marker_cannot_point_at_foreign_feed(self):
        d = self.remote()
        d["config"]["settings"]["feed_urls"] = "https://example.org/unrelated.atom"
        with self.assertRaises(ValueError):
            self.load(d)

    def test_marker_cannot_point_at_foreign_resource_id(self):
        d = self.remote()
        d["id"] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            self.load(d)

    def test_marker_cannot_change_local_kb_or_url(self):
        for key, val in (("knowledge_base_id", str(uuid.uuid4())),
                         ("source_url", "https://example.org/foreign.atom")):
            original = json.loads(self.marker.read_text())
            changed = dict(original, **{key: val})
            self.marker.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                self.load()
            self.marker.write_text(json.dumps(original))

    def test_symlink_and_world_readable_marker_are_rejected(self):
        other = self.root / "other.json"
        other.write_text(self.marker.read_text())
        self.marker.unlink()
        self.marker.symlink_to(other)
        with self.assertRaises((ValueError, OSError)):
            self.load()
        self.marker.unlink()
        self.marker.write_text(other.read_text())
        self.marker.chmod(0o644)
        with self.assertRaises(ValueError):
            self.load()

    def test_native_datasource_must_be_paused_rss_incremental(self):
        for key, val in (("type", "mail"), ("sync_deletions", True),
                         ("status", "active"), ("sync_mode", "full")):
            d = self.remote()
            d[key] = val
            with self.assertRaises(ValueError):
                self.load(d)


if __name__ == "__main__":
    unittest.main()
