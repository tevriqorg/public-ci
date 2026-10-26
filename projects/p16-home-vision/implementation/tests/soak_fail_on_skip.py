"""Fail registered soak pytest commands when a selected test did not execute."""

import pytest


_skipped_reports = []


def pytest_runtest_logreport(report):
    if report.skipped:
        _skipped_reports.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _skipped_reports:
        unique = sorted(set(_skipped_reports))
        print(f"P16_SOAK_SKIPPED_TESTS_FORBIDDEN count={len(unique)}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
