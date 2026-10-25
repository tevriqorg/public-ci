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
    target = publish_immutable_package(tmp_path / "packages" / "pkg-1", package, artifact_root=root)
    assert json.loads(target.read_text())["artifacts"][0]["sha256"]
    assert publish_immutable_package(target.parent, package, artifact_root=root) == target
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


def test_s023_published_artifact_snapshot_survives_transcript_retry(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    transcript = source / "transcript.json"
    original = b'{"schema":"p16.transcript.v1","revision":1}\n'
    retry = b'{"schema":"p16.transcript.v1","revision":2}\n'
    transcript.write_bytes(original)

    package = build_analysis_package(
        package_id="s023-package", asset_id="s023-asset",
        source_manifest={"source_id": "synthetic-s023"},
        pipeline={"revision": "p16-core-s023"},
        artifacts=[{"artifact_id": "transcript-s023", "artifact_type": "transcript",
                    "path": "transcript.json"}],
        artifact_root=source,
    )
    package_path = publish_immutable_package(
        tmp_path / "packages", package, artifact_root=source
    )
    manifest_bytes = package_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    artifact = manifest["artifacts"][0]
    snapshot = (package_path.parent / artifact["path"]).resolve()

    assert snapshot.is_relative_to(package_path.parent.resolve())
    assert snapshot.read_bytes() == original
    transcript.write_bytes(retry)

    assert transcript.read_bytes() == retry
    assert package_path.read_bytes() == manifest_bytes
    assert snapshot.read_bytes() == original
    assert len(original) == artifact["size_bytes"]
    assert __import__("hashlib").sha256(snapshot.read_bytes()).hexdigest() == artifact["sha256"]
    # Re-publishing the exact revision stays idempotent even after the source path
    # is replaced by a retry; it never aliases the new transcript bytes.
    assert publish_immutable_package(
        package_path.parent, package, artifact_root=source
    ) == package_path
