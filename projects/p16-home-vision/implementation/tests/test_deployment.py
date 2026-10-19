from pathlib import Path

import pytest

from scripts.preflight_local_storage import PreflightError, run_preflight


ROOT = Path(__file__).resolve().parents[1]


def test_fixture_preflight_smokes_state_and_two_archive_paths(tmp_path) -> None:
    state = tmp_path / "state"
    archive_a = tmp_path / "archive-a"
    archive_b = tmp_path / "archive-b"
    for path in (state, archive_a, archive_b):
        path.mkdir()

    result = run_preflight(state, archive_a, archive_b)

    assert result.verdict == "PASS"
    assert result.state.sqlite_wal_smoke is True
    assert result.state.writable_smoke is True
    assert result.archive_a.writable_smoke is True
    assert result.archive_b.writable_smoke is True
    assert result.distinct_archive_paths is True
    assert list(state.iterdir()) == []
    assert list(archive_a.iterdir()) == []
    assert list(archive_b.iterdir()) == []


def test_preflight_fails_closed_on_collapsed_storage_paths(tmp_path) -> None:
    state = tmp_path / "state"
    archive = tmp_path / "archive"
    state.mkdir()
    archive.mkdir()

    with pytest.raises(PreflightError, match="archive_a_and_b_must_be_distinct_paths"):
        run_preflight(state, archive, archive)

    with pytest.raises(PreflightError, match="state_must_not_share_archive_directory"):
        run_preflight(archive, archive, state)


def test_compose_keeps_state_archive_and_ingress_boundaries_separate() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "HOME_VISION_STATE_DIR" in compose
    assert "HOME_VISION_ARCHIVE_A" in compose
    assert "HOME_VISION_ARCHIVE_B" in compose
    assert "sqlite:////state/home-vision.db" in compose
    assert "target: /state" in compose
    assert "target: /archive/a" in compose
    assert "target: /archive/b" in compose
    assert '"${HOME_VISION_BIND_ADDRESS:-127.0.0.1}:8000:8000"' in compose
    assert '\n      - "8000:8000"' not in compose
    assert "cap_drop:" in compose
    assert "no-new-privileges:true" in compose
    assert 'HOME_VISION_ARCHIVE_AUTO_ENQUEUE: "${HOME_VISION_ARCHIVE_AUTO_ENQUEUE:-false}"' in compose
    assert 'HOME_VISION_NVR_TIMEZONE_VERIFIED: "${HOME_VISION_NVR_TIMEZONE_VERIFIED:-false}"' in compose
    assert 'HOME_VISION_NVR_SCAN_INITIAL_DAYS: "${HOME_VISION_NVR_SCAN_INITIAL_DAYS:-60}"' in compose


def test_environment_example_contains_no_secret_or_nvr_credentials() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8").lower()
    forbidden_assignments = ("password=", "token=", "secret=", "username=", "nvr_host=", "nvr_url=")
    assert not any(value in example for value in forbidden_assignments)
    assert "home_vision_bind_address=127.0.0.1" in example
    assert "home_vision_state_dir=" in example
    assert "home_vision_archive_a=" in example
    assert "home_vision_archive_b=" in example
    assert "home_vision_archive_auto_enqueue=false" in example
    assert "home_vision_nvr_timezone_verified=false" in example
    assert "home_vision_nvr_scan_min_date=" in example


def test_dockerfile_is_non_root_and_contains_ffmpeg_and_healthcheck() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12-slim" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'ENTRYPOINT ["/srv/home-vision/container-entrypoint.sh"]' in dockerfile


def test_entrypoint_migrates_before_starting_application() -> None:
    entrypoint = (ROOT / "container-entrypoint.sh").read_text(encoding="utf-8")
    migration_index = entrypoint.index('"alembic", "upgrade", "head"')
    exec_index = entrypoint.index('exec "$@"')
    assert migration_index < exec_index
    assert "fcntl.flock" in entrypoint
    assert ".alembic-migration.lock" in entrypoint
