"""S015: execute the real pure product function and an order-sensitive mutant."""
import ast
import hashlib
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PRODUCT = Path(__file__).resolve().parents[1] / "app/domain/nvr_scan.py"


def load_hash(source):
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_response_hash")
    module = ast.Module(body=[function], type_ignores=[])
    env = {"hashlib": hashlib, "DiscoveredSegment": SimpleNamespace}
    exec(compile(ast.fix_missing_locations(module), str(PRODUCT), "exec"), env)
    return env["_response_hash"]


def corpus():
    start = datetime(2026, 10, 23, tzinfo=timezone.utc)
    return [SimpleNamespace(track_id=str(101+i), source_start=start+timedelta(hours=i),
                            source_end=start+timedelta(hours=i,minutes=3),
                            locator_fingerprint=hashlib.sha256(str(i).encode()).hexdigest())
            for i in range(3)]


def order_invariant(function):
    return len({function(list(p)) for p in itertools.permutations(corpus())}) == 1


class ScanMutationTests(unittest.TestCase):
    def test_S015_actual_product_hash_is_order_independent(self):
        self.assertTrue(order_invariant(load_hash(PRODUCT.read_text())))

    def test_S015_changed_source_is_not_stable(self):
        function = load_hash(PRODUCT.read_text())
        before = corpus()
        after = corpus()
        after[0].source_end += timedelta(seconds=1)
        self.assertNotEqual(function(before), function(after))
        after = corpus()
        after[0].locator_fingerprint = "changed-fingerprint"
        self.assertNotEqual(function(before), function(after))

    def test_S015_real_order_sensitive_mutant_is_killed(self):
        source = PRODUCT.read_text()
        tree = ast.parse(source)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=="_response_hash")
        # Reintroduce the actual defect: iterate input rows without canonical ordering.
        loop = next(n for n in function.body if isinstance(n, ast.For))
        loop.iter = ast.Name(id="segments", ctx=ast.Load())
        mutant = ast.unparse(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])))
        self.assertFalse(order_invariant(load_hash(mutant)))

if __name__ == "__main__": unittest.main()
