from __future__ import annotations

from app.web.runtime import render_runtime_html


def test_b_balanced_dark_glass_visual_tokens_are_active() -> None:
    html = render_runtime_html()
    assert "P16R1 B — Balanced Dark Glass" in html
    assert "P16R1 C-direction implementation baseline" not in html
    assert "--bg:#1b2a38" in html
    assert "linear-gradient(145deg,#182633,#213446 48%,#1a2b39)" in html
    assert "rgba(52,70,85,.90)" in html
    assert "background:linear-gradient(145deg,#edf3f5,#dce7eb)" in html
    assert "linear-gradient(145deg,#0b1722,#102536 48%,#0c1b28)" not in html


def test_desktop_primary_navigation_remains_left_vertical_rail() -> None:
    html = render_runtime_html()
    assert ".app { grid-template-columns:94px 1fr; }" in html
    assert ".side { background:linear-gradient" in html
    assert ".nav button { text-align:center" in html
    assert "data-page=\"overview\"" in html
    assert "data-page=\"archive\"" in html
    assert "data-page=\"analysis\"" in html
    assert "data-page=\"tasks\"" in html
    assert "data-page=\"review\"" in html
    assert "data-page=\"history\"" in html
    assert 'class="topnav"' not in html


def test_b_revision_keeps_existing_stage4_interaction_contracts() -> None:
    html = render_runtime_html()
    assert "thumb-loading" in html
    assert "正在载入封面…" in html
    assert ".player.large" in html
    assert ".player.full" in html
    assert "data-player-prev" in html
    assert "data-player-next" in html
    assert "archiveScrollTop" in html
