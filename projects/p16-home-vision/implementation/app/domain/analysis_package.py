from __future__ import annotations

import hashlib
import json
import errno
import os
import shutil
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


def _atomic_snapshot(
    package_dir: Path, package_revision: str, package: dict[str, Any], artifact_root: Path
) -> None:
    snapshot_root = package_dir / "artifact-snapshots" / package_revision
    snapshot_root.parent.mkdir(parents=True, exist_ok=True)
    artifacts = package.get("artifacts", [])

    def verified(path: Path, item: dict[str, Any]) -> bool:
        return (path.is_file() and path.stat().st_size == item["size_bytes"]
                and _sha256(path) == item["sha256"])

    if snapshot_root.is_symlink():
        raise AnalysisPackageError("PACKAGE_SNAPSHOT_CORRUPT")
    if snapshot_root.exists():
        _verify_existing_snapshot(package_dir, package_revision, artifacts)
        return

    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=snapshot_root.parent))
    try:
        for item in artifacts:
            rel = item.get("path")
            if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
                raise AnalysisPackageError("PACKAGE_ARTIFACT_PATH_UNSAFE")
            try:
                source = (artifact_root / rel).resolve(strict=True)
            except OSError as exc:
                raise AnalysisPackageError("PACKAGE_ARTIFACT_MISSING") from exc
            if not source.is_relative_to(artifact_root) or not source.is_file():
                raise AnalysisPackageError("PACKAGE_ARTIFACT_MISSING")
            if not verified(source, item):
                raise AnalysisPackageError("PACKAGE_ARTIFACT_CHANGED")
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if not verified(destination, item):
                raise AnalysisPackageError("PACKAGE_SNAPSHOT_VERIFY_FAILED")
        for directory, child_dirs, files in os.walk(staging, topdown=False):
            for filename in files:
                os.chmod(Path(directory) / filename, 0o444)
            os.chmod(directory, 0o555)
        try:
            os.rename(staging, snapshot_root)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            _verify_existing_snapshot(package_dir, package_revision, artifacts)
        directory = os.open(snapshot_root.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if staging.exists():
            for directory, _, _ in os.walk(staging, topdown=False):
                os.chmod(directory, 0o755)
            shutil.rmtree(staging)


def _verify_existing_snapshot(package_dir: Path, package_revision: str,
                              artifacts: list[dict[str, Any]]) -> None:
    snapshot_root = package_dir / "artifact-snapshots" / package_revision
    if snapshot_root.is_symlink() or not snapshot_root.is_dir():
        raise AnalysisPackageError("PACKAGE_SNAPSHOT_CORRUPT")
    root = snapshot_root.resolve(strict=True)
    for item in artifacts:
        rel = item.get("path")
        if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise AnalysisPackageError("PACKAGE_ARTIFACT_PATH_UNSAFE")
        path = snapshot_root / rel
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise AnalysisPackageError("PACKAGE_SNAPSHOT_CORRUPT") from exc
        if (path.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file()
                or resolved.stat().st_size != item["size_bytes"]
                or _sha256(resolved) != item["sha256"]):
            raise AnalysisPackageError("PACKAGE_SNAPSHOT_CORRUPT")


def publish_immutable_package(
    package_dir: Path, package: dict[str, Any], *, artifact_root: Path | None = None
) -> Path:
    """Publish a manifest with package-local, content-verified artifact snapshots."""
    if package.get("schema") != PACKAGE_SCHEMA:
        raise AnalysisPackageError("PACKAGE_SCHEMA_UNSUPPORTED")
    package_dir.mkdir(parents=True, exist_ok=True)
    target = package_dir / "analysis-package.json"
    original = json.dumps(package, sort_keys=True, separators=(",", ":")).encode()
    package_revision = hashlib.sha256(original).hexdigest()
    published = json.loads(original)
    for item in published.get("artifacts", []):
        rel = item.get("path")
        if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise AnalysisPackageError("PACKAGE_ARTIFACT_PATH_UNSAFE")
        item["path"] = (Path("artifact-snapshots") / package_revision / Path(rel)).as_posix()
    body = (json.dumps(published, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if target.exists():
        if target.read_bytes() == body:
            if package.get("artifacts"):
                _verify_existing_snapshot(package_dir, package_revision, package["artifacts"])
            return target
        raise AnalysisPackageError("PACKAGE_IMMUTABLE_CONFLICT")
    if package.get("artifacts"):
        if artifact_root is None:
            raise AnalysisPackageError("PACKAGE_ARTIFACT_ROOT_REQUIRED")
        root = artifact_root.resolve(strict=True)
        _atomic_snapshot(package_dir, package_revision, package, root)
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
