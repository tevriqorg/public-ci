"""#126 full-suite upstream cache must not share a mutable cross-user /tmp git tree."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.contracts.upstream_fixture import _cache_root


class CacheSafety(unittest.TestCase):
    def test_default_cache_is_private_and_per_user(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"AIBOX_UPSTREAM_CACHE": ""}):
                with patch("tests.contracts.upstream_fixture.tempfile.gettempdir", return_value=td):
                    root = _cache_root()
                    self.assertIn(str(os.getuid()), root.name)
                    self.assertEqual(root.stat().st_mode & 0o077, 0)
                    self.assertEqual(root.stat().st_uid, os.geteuid())

    def test_refuse_symlink_and_world_readable_explicit_cache(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            unsafe = base / "shared"
            unsafe.mkdir(mode=0o755)
            unsafe.chmod(0o755)
            with patch.dict(os.environ, {"AIBOX_UPSTREAM_CACHE": str(unsafe)}):
                with self.assertRaises(ValueError):
                    _cache_root()
            link = base / "alias"
            link.symlink_to(unsafe)
            with patch.dict(os.environ, {"AIBOX_UPSTREAM_CACHE": str(link)}):
                with self.assertRaises(ValueError):
                    _cache_root()


if __name__ == "__main__":
    unittest.main()
