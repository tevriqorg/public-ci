import os
from pathlib import Path
import subprocess
import sys


def test_soak_wrapper_rejects_missing_parameterized_case(tmp_path):
    test_file = tmp_path / "test_one_case.py"
    test_file.write_text("def test_only_remaining_case():\n    assert True\n")
    implementation = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["P16_SOAK_EXPECTED_TEST_COUNT"] = "2"
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(implementation), environment.get("PYTHONPATH")])
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.soak_fail_on_skip", str(test_file)],
        cwd=implementation,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "P16_SOAK_EXPECTED_TEST_COUNT_MISMATCH expected=2 actual=1" in result.stdout
