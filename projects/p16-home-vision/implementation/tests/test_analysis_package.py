import json

import pytest

from app.domain.analysis_package import AnalysisPackageError, build_analysis_package, publish_immutable_package


def test_package_is_hash_manifest_and_immutable(tmp_path):
    root = tmp_path / "artifact-root"
    root.mkdir()
    (root / "transcript.json").write_text('{"schema":"p16.transcript.v1"}\n')
    package = build_analysis_package(
        package_id="pkg-1", asset_id="asset-1", source_manifest={"source_id": "source-1"},
        pipeline={"version": "p16-core-v1"},
        artifacts=[{"artifact_id": "transcript-1", "artifact_type": "transcript", "path": "transcript.json"}],
        artifact_root=root,
    )
    target = publish_immutable_package(tmp_path / "packages" / "pkg-1", package)
    assert json.loads(target.read_text())["artifacts"][0]["sha256"]
    assert publish_immutable_package(target.parent, package) == target
    package["pipeline"]["version"] = "changed"
    with pytest.raises(AnalysisPackageError, match="PACKAGE_IMMUTABLE_CONFLICT"):
        publish_immutable_package(target.parent, package)


def test_package_rejects_escape_and_missing_artifacts(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    with pytest.raises(AnalysisPackageError, match="PACKAGE_ARTIFACT_PATH_UNSAFE"):
        build_analysis_package(package_id="p", asset_id="a", source_manifest={}, pipeline={}, artifacts=[{"artifact_id":"x","artifact_type":"x","path":"../outside"}], artifact_root=root)
    with pytest.raises(AnalysisPackageError, match="PACKAGE_ARTIFACT_MISSING"):
        build_analysis_package(package_id="p", asset_id="a", source_manifest={}, pipeline={}, artifacts=[{"artifact_id":"x","artifact_type":"x","path":"missing"}], artifact_root=root)
