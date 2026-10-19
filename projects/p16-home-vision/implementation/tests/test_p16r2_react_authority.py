from pathlib import Path


FRONTEND_ROOT = Path(__file__).parents[1] / "frontend-r2"
APP_SOURCE = FRONTEND_ROOT / "src" / "App.tsx"
BEHAVIOR_SOURCE = FRONTEND_ROOT / "src" / "archiveBehavior.js"


def _app_source() -> str:
    return APP_SOURCE.read_text(encoding="utf-8")


def _behavior_source() -> str:
    return BEHAVIOR_SOURCE.read_text(encoding="utf-8")


def test_p16r2_react_preserves_six_page_product_ia_and_calendar_first_overview() -> None:
    source = _app_source()
    labels = ["总览", "归档", "分析", "任务", "待复核", "记录"]
    positions = [source.index(f"label: '{label}'") for label in labels]
    assert positions == sorted(positions)
    assert 'className="rail"' in source
    assert 'className="calendar-panel"' in source
    assert "calendar-grid" in source
    assert "recordingsByDay" in source


def test_p16r2_react_uses_resource_refresh_and_preserves_local_context() -> None:
    source = _app_source()
    assert "queryKey: ['product', 'catalog']" in source
    assert "refetchInterval: 5000" in source
    assert "placeholderData: (previous) => previous" in source
    assert "next_after_id" in source
    assert "selectedDay" in source
    assert "expanded" in source
    assert "player" in source


def test_p16r2_react_tasks_are_backend_authoritative_and_truthful() -> None:
    source = _app_source()
    for action in ("pause_queued", "cancel_waiting", "resume_paused", "retry_failed"):
        assert action in source
    for endpoint in (
        "/internal/archive/jobs/pause-all",
        "/internal/archive/jobs/cancel-unfinished",
        "/internal/archive/jobs/resume-all",
        "/internal/archive/jobs/retry-failed",
    ):
        assert endpoint in source
    assert "control_actions" in source
    assert "进度百分比未知" in source
    assert "indeterminate" in source


def test_p16r2_react_archive_preview_and_player_use_backend_truth() -> None:
    source = _app_source()
    behavior = _behavior_source()
    assert "camera-lane" in source
    assert "预览当天可归档录像" in source
    assert "确认并加入队列" in source
    assert "/internal/archive/batch" in source
    assert "/api/v1/media/" in source
    assert "FloatingPlayer" in source
    assert "onStep(-1)" in source
    assert "onStep(1)" in source
    assert "playback_path" in behavior
    assert "access.status !== 'available'" in behavior
