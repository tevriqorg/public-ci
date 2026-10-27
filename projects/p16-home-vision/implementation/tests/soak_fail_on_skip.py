"""Fail registered soak pytest commands when a selected test did not execute."""

import os

import pytest


_skipped_reports = []
_call_reports = []


def pytest_runtest_logreport(report):
    if report.skipped:
        _skipped_reports.append(report.nodeid)
    if report.when == "call":
        _call_reports.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    expected_raw = os.environ.get("P16_SOAK_EXPECTED_TEST_COUNT")
    if expected_raw is not None:
        try:
            expected = int(expected_raw)
        except ValueError:
            expected = -1
        if expected < 1 or len(_call_reports) != expected:
            print(f"P16_SOAK_EXPECTED_TEST_COUNT_MISMATCH expected={expected_raw} actual={len(_call_reports)}")
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if _skipped_reports:
        unique = sorted(set(_skipped_reports))
        print(f"P16_SOAK_SKIPPED_TESTS_FORBIDDEN count={len(unique)}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
