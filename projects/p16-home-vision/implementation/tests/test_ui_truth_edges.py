from __future__ import annotations

from app.web.runtime import render_runtime_html


def test_partial_scan_coverage_is_never_promoted_to_full_scan() -> None:
    html = render_runtime_html()
    assert "states.every((scanState) => scanState === 'success')" in html
    assert "states.some((scanState) => ['success', 'partial'].includes(scanState))" in html
    assert "const successful = required.filter" not in html


def test_unknown_source_state_is_not_archive_eligible() -> None:
    html = render_runtime_html()
    assert "if (recording.source_state === 'available') return 'eligible';" in html
    assert "return 'unavailable';" in html
    assert "源状态待确认" in html
    assert "源待确认" in html


def test_day_is_complete_only_when_every_recording_is_archived() -> None:
    html = render_runtime_html()
    assert "dayRecordings.every((recording) => archiveDisposition(recording) === 'archived')" in html
    assert "dayHasUnavailable" in html
    assert "暂无可归档录像" in html


def test_stale_selection_is_pruned_to_current_archive_candidates() -> None:
    html = render_runtime_html()
    assert "selectableArchiveIds" in html
    assert "if (!selectableArchiveIds.has(id)) selectedArchiveIds.delete(id)" in html


def test_hydrate_coalesces_refresh_intents_instead_of_dropping_them() -> None:
    html = render_runtime_html()
    assert "let pendingHydrateIntent = null;" in html
    assert "pendingHydrateIntent = mergeHydrateIntent" in html
    assert "if (nextIntent) await hydrate(nextIntent)" in html
