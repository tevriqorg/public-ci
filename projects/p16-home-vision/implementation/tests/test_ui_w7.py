from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app.main import app
from app.web.runtime import BASELINE_PATH, RUNTIME_SCRIPT_MARKER, render_runtime_html


APPROVED_R11_BLOB = "7ae14d920746b866d915a24bfcec76ab26070722"


def git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("utf-8")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - Git object identity is SHA-1 by definition.


def test_frozen_r11_baseline_is_exact_approved_git_blob() -> None:
    raw = BASELINE_PATH.read_bytes()
    assert git_blob_sha(raw) == APPROVED_R11_BLOB


def test_runtime_preserves_exact_primary_navigation_and_focus_contract() -> None:
    html = render_runtime_html()
    for label in ("总览", "归档", "分析", "任务", "待复核", "记录"):
        assert label in html
    assert 'id="playerClose" aria-label="关闭播放器"' in html
    assert "focusPlayer" in html
    assert "document.getElementById('playerClose')?.focus({preventScroll: true})" in html
    assert "analysisDetail').classList.remove('show')" in html
    assert "openPlayer(`${run.asset_id} · ${formatOffset(event.start_offset_ms)}`, true)" in html


def test_runtime_reconciles_obsolete_scene_and_prototype_claims() -> None:
    html = render_runtime_html()
    assert "Scene 已同步" not in html


def test_runtime_does_not_ship_fixture_values_before_api_hydration() -> None:
    html = render_runtime_html()
    for fixture_value in ("2026年8月", "2.3 TB", "任务 7", "06:00–07:12", "Qwen3-VL-8B", "A8 Host"):
        assert fixture_value not in html
    for fixture_value in ("今天已分析</div><div class=\"v\">31", "发现事件</div><div class=\"v\">47", "<option>客厅</option><option>卧室</option>"):
        assert fixture_value not in html
    assert 'id="taskCount">读取中<' in html
    assert "AIBOX Connector · Scene sync" not in html
    assert "AIBOX 同步失败" not in html
    assert "状态演示 · 原型" not in html
    assert '<aside class="statelab" id="lab">' not in html
    assert "profile_snapshot_hash" in html
    assert "分析策略" in html
    assert "Integration / Consumer API" in html
    assert "analysisRuns" not in html
    assert "queuedExtra" not in html
    assert "约 91 GB" not in html


def test_runtime_hydrates_real_consumer_api_surfaces_without_private_locators() -> None:
    html = render_runtime_html()
    for path in (
        "/api/v1/cameras",
        "/api/v1/recordings",
        "/api/v1/scan-runs",
        "/api/v1/assets",
        "/api/v1/storage-volumes",
        "/api/v1/analysis-profiles",
        "/api/v1/analysis-runs",
        "/api/v1/events",
        "/api/v1/review-flags",
        "/api/v1/audit-events",
    ):
        assert path in html
    assert "safeJson('/api/v1/capabilities')" in html
    assert "fetchAll('/api/v1/archive-jobs', 500)" in html
    assert "hydrateOverview" in html
    assert "hydrateAnalysis" in html
    assert "analysis_profile_snapshot_hash" in html
    assert "/fixture/" not in html
    assert "mount_path_runtime" not in html
    assert "playback_uri" not in html
    assert "playback_path" in html
    assert "password" not in html.lower()


def test_runtime_uses_real_archive_enqueue_contract_without_fixture_impact_preview() -> None:
    html = render_runtime_html()
    assert "postJson('/internal/archive/batch'" in html
    assert "/internal/archive/status" in html
    assert "archiveStatus?.enqueue_ready" in html
    assert "批量拉取" in html
    assert "worker 按实际可写空间选择归档卷" in html
    assert "失败可重试" in html
    assert "不会伪造归档成功" in html
    assert "约 91 GB" not in html
    assert "失败重试</span><b>3 段" not in html


def test_runtime_uses_explicit_product_storage_a_b_labels() -> None:
    html = render_runtime_html()
    assert "const label = `归档盘 ${index === 0 ? 'A' : 'B'}`" in html
    assert "esc(volume.storage_volume_id)}</b>" in html
    assert "volume.physical_device_id" in html
    assert "存储身份：" in html
    assert ">physical_device_id<" not in html


def test_runtime_refreshes_task_badge_catalog_and_live_progress_without_losing_context() -> None:
    html = render_runtime_html()
    assert "window.setInterval" in html
    assert "cache: 'no-store'" in html
    assert "jobSpeed" in html
    assert "progress_percent" in html
    assert "source_size_bytes" in html
    assert "resolving_source" in html
    assert "downloading" in html
    assert "verifying" in html
    assert "refreshCatalog: true" in html
    assert "fetchAll('/api/v1/recordings', 2000, recordings.next_after_id)" in html
    assert "safeJson('/api/v1/recordings?limit=100')" in html
    assert "recordings.next_after_id" in html
    assert "state.recordings = recordings.items || [];" in html
    assert "const [assets, volumes] = await Promise.all" in html
    assert "CATALOG_REFRESH_MS = 15000" in html
    assert "JOB_REFRESH_MS = 15000" in html
    assert "lastCatalogRefreshAt" in html
    assert "SECONDARY_REFRESH_MS = 15000" in html
    assert "secondaryLoadedAt" in html
    assert "task-history" in html
    assert "tasks.classList.contains('active')" in html
    assert "renderTruthfulSurfaces," in html
    assert "archiveControlActions.has('pause_queued')" in html
    assert "archiveControlActions.has('cancel_waiting')" in html
    assert "document.visibilityState !== 'hidden' && !hydrateInFlight" in html
    assert "}, 5000);" in html
    assert "/internal/archive/jobs/pause-all" in html
    assert "/internal/archive/jobs/resume-all" in html
    assert "/internal/archive/jobs/retry-failed" in html
    assert "/internal/archive/jobs/cancel-unfinished" in html


def test_runtime_distinguishes_scan_coverage_from_recording_boundaries() -> None:
    html = render_runtime_html()
    assert "/api/v1/scan-runs" in html
    assert "knownScanDays" in html
    assert "earliestKnownDate" in html
    assert "已知数据边界" in html
    assert "已查询 · 无录像" in html
    assert "尚未查询 NVR" in html
    assert "查询失败 · 尚未确认" in html
    assert "查询失败 · 可重试" not in html


def test_runtime_repairs_page_padding_and_empty_state() -> None:
    html = render_runtime_html()
    assert "_repair_prototype_page_nesting" not in html
    assert "暂无分析结果" in html
    assert ".content > .page" in html


def test_runtime_keeps_overview_sections_outside_primary_calendar_grid() -> None:
    html = render_runtime_html()
    overview_start = html.index('<div class="overview">')
    storage_start = html.index('<div class="storage">', overview_start)
    assert html.index('</div></div><div class="storage">', overview_start) < storage_start


def test_fastapi_serves_production_ui_shell() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert RUNTIME_SCRIPT_MARKER in response.text
    assert "Home Vision" in response.text



def test_runtime_implements_current_visual_direction_and_real_multi_select_preview() -> None:
    html = render_runtime_html()
    assert "P16R1 B — Balanced Dark Glass" in html
    assert "linear-gradient(145deg,#182633,#213446 48%,#1a2b39)" in html
    assert "rgba(52,70,85,.90)" in html
    assert "selectedArchiveIds" in html
    assert "data-select-recording" in html
    assert "queueArchiveSelection" in html
    assert "拉取全部当前可拉取" in html
    assert "确认加入任务" in html


def test_runtime_keeps_worker_configuration_and_readiness_separate() -> None:
    html = render_runtime_html()
    assert "archiveStatus?.enqueue_ready" in html
    assert "archiveConfigured" in html
    assert "archiveControlActions" in html
    for action in ("resume_paused", "retry_failed", "pause_queued", "cancel_waiting"):
        assert action in html


def test_runtime_preserves_progressive_archive_and_player_navigation() -> None:
    html = render_runtime_html()
    assert "archiveVisibleDateCount = 1" in html
    assert "IntersectionObserver" in html
    assert "data-thumb-src" in html
    assert "rootMargin: '700px 0px'" in html
    assert "thumb-shell" in html
    assert "正在载入封面…" in html
    assert "封面暂不可用" in html
    assert "window.scrollTo({top: 0, behavior: 'auto'})" in html
    assert "data-player-prev" in html
    assert "data-player-next" in html
    assert "mediaRequestId" in html
    assert "archiveScrollTop" in html


def test_runtime_preserves_local_quality_fixes_for_thumbnails_playback_and_progress() -> None:
    html = render_runtime_html()
    assert "/api/v1/source-recordings/" in html
    assert "recording.thumbnail_available" in html
    assert "THUMBNAIL_CONCURRENCY = 4" in html
    assert "const patchArchiveProgress" in html
    assert "invalidateMediaRequest" in html
    assert "normalization_state === 'completed'" in html


def test_runtime_delete_review_is_preview_only_without_mutation_api() -> None:
    html = render_runtime_html()
    assert "data-delete-preview" in html
    assert "当前 Cloud runtime 未暴露物理删除 mutation API" in html
    assert "Tombstone / Audit" in html
    assert "真实物理删除在具备受控 mutation API 后才可执行" in html



def test_partial_scan_never_becomes_full_scan_coverage() -> None:
    html = render_runtime_html()
    assert "states.every((scanState) => scanState === 'success')" in html
    assert "states.some((scanState) => ['success', 'partial'].includes(scanState))" in html
    assert "const successful = required.filter" not in html


def test_unknown_source_is_not_archive_eligible_or_day_complete() -> None:
    html = render_runtime_html()
    assert "if (recording.source_state === 'available') return 'eligible';" in html
    assert "return 'unavailable';" in html
    assert "dayRecordings.every((recording) => archiveDisposition(recording) === 'archived')" in html
    assert "源状态待确认" in html
    assert "源待确认" in html
