from __future__ import annotations

import json

import pytest

from app.domain.frame_reduce import FrameManifestError, load_working_frame_manifest


def test_manifest_uses_pts_not_frame_index(tmp_path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "index": 50,
                        "file": "00000050.jpg",
                        "source_pts_seconds": 1.25,
                        "absolute_epoch_seconds": 1_800_000_001.25,
                    }
                ),
                json.dumps(
                    {
                        "index": 51,
                        "file": "00000051.jpg",
                        "source_pts_seconds": 3.875,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    frames = load_working_frame_manifest(manifest, asset_id="asset-1")

    assert frames[0].artifact_id == "asset-1:frame:50"
    assert frames[0].source_offset_ms == 1250
    assert frames[1].source_offset_ms == 3875
    assert frames[0].absolute_epoch_seconds == pytest.approx(1_800_000_001.25)


def test_manifest_rejects_missing_pts(tmp_path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({"index": 1, "file": "00000001.jpg"}), encoding="utf-8")

    with pytest.raises(FrameManifestError) as excinfo:
        load_working_frame_manifest(manifest, asset_id="asset-1")

    assert excinfo.value.code == "FRAME_MANIFEST_PTS_REQUIRED"


def test_manifest_rejects_non_monotonic_pts(tmp_path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join(
            [
                json.dumps({"index": 1, "file": "1.jpg", "source_pts_seconds": 2.0}),
                json.dumps({"index": 2, "file": "2.jpg", "source_pts_seconds": 1.0}),
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(FrameManifestError) as excinfo:
        load_working_frame_manifest(manifest, asset_id="asset-1")

    assert excinfo.value.code == "FRAME_MANIFEST_PTS_NOT_MONOTONIC"
