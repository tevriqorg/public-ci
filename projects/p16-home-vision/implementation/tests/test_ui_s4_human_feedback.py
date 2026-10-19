from __future__ import annotations

from app.web.runtime import render_runtime_html


def test_stage4_b_balanced_dark_glass_and_vertical_navigation_contract() -> None:
    html = render_runtime_html()
    assert "P16R1 B — Balanced Dark Glass" in html
    assert "--bg:#1b2a38" in html
    assert "linear-gradient(145deg,#182633,#213446 48%,#1a2b39)" in html
    assert "rgba(52,70,85,.90)" in html
    assert "background:linear-gradient(145deg,#edf3f5,#dce7eb)" in html
    assert ".app { grid-template-columns:94px 1fr; }" in html
    assert ".side { background:linear-gradient" in html
    assert ".nav button { text-align:center" in html
    assert "linear-gradient(145deg,#0b1722,#102536 48%,#0c1b28)" not in html


def test_archive_thumbnail_has_loading_loaded_and_failure_contracts() -> None:
    html = render_runtime_html()
    assert "thumb-shell" in html
    assert "thumb-loading" in html
    assert "thumb-spinner" in html
    assert "正在载入封面…" in html
    assert "封面已载入" in html
    assert "封面暂不可用" in html
    assert "armThumbnail" in html
    assert "revealThumbnail" in html
    assert "rootMargin: '700px 0px'" in html


def test_formal_player_keeps_large_full_and_previous_next_controls() -> None:
    html = render_runtime_html()
    assert ".player.large" in html
    assert ".player.full" in html
    assert "data-player-prev" in html
    assert "data-player-next" in html
    assert "toggleFull()" in html


def test_primary_navigation_does_not_inherit_previous_page_scroll() -> None:
    html = render_runtime_html()
    assert "document.querySelectorAll('[data-page]')" in html
    assert "window.scrollTo({top: 0, behavior: 'auto'})" in html
    assert "archiveScrollTop" in html
