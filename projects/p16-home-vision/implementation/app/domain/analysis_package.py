from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


PACKAGE_SCHEMA = "p16.analysis_package.v1"


class AnalysisPackageError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_analysis_package(
    *, package_id: str, asset_id: str, source_manifest: dict[str, Any],
    pipeline: dict[str, Any], artifacts: list[dict[str, Any]], artifact_root: Path,
) -> dict[str, Any]:
    if not package_id or not asset_id:
        raise AnalysisPackageError("PACKAGE_IDENTITY_REQUIRED")
    root = artifact_root.resolve(strict=True)
    entries: list[dict[str, Any]] = []
    for item in artifacts:
        rel = item.get("path")
        if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise AnalysisPackageError("PACKAGE_ARTIFACT_PATH_UNSAFE")
        try:
            path = (root / rel).resolve(strict=True)
        except OSError as exc:
            raise AnalysisPackageError("PACKAGE_ARTIFACT_MISSING") from exc
        if not path.is_relative_to(root) or not path.is_file():
            raise AnalysisPackageError("PACKAGE_ARTIFACT_MISSING")
        entries.append({
            "artifact_id": str(item["artifact_id"]), "artifact_type": str(item["artifact_type"]),
            "path": Path(rel).as_posix(), "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    if len({entry["artifact_id"] for entry in entries}) != len(entries):
        raise AnalysisPackageError("PACKAGE_ARTIFACT_ID_DUPLICATE")
    return {
        "schema": PACKAGE_SCHEMA,
        "package_id": package_id,
        "asset_id": asset_id,
        "source": source_manifest,
        "pipeline": pipeline,
        "artifacts": sorted(entries, key=lambda row: row["artifact_id"]),
    }


def publish_immutable_package(package_dir: Path, package: dict[str, Any]) -> Path:
    """Publish a canonical package atomically; existing content is immutable."""
    if package.get("schema") != PACKAGE_SCHEMA:
        raise AnalysisPackageError("PACKAGE_SCHEMA_UNSUPPORTED")
    package_dir.mkdir(parents=True, exist_ok=True)
    target = package_dir / "analysis-package.json"
    body = (json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if target.exists():
        if target.read_bytes() == body:
            return target
        raise AnalysisPackageError("PACKAGE_IMMUTABLE_CONFLICT")
    with tempfile.NamedTemporaryFile(dir=package_dir, prefix=".package-", delete=False) as temp:
        temp.write(body)
        temp.flush()
        os.fsync(temp.fileno())
        temporary = Path(temp.name)
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != body:
                raise AnalysisPackageError("PACKAGE_IMMUTABLE_CONFLICT")
        return target
    finally:
        temporary.unlink(missing_ok=True)
