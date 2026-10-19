from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.models import LocalAsset, StorageVolume


@dataclass(frozen=True)
class MediaAccessDecision:
    status: str
    path: Path | None = None


def resolve_media_path(volume_root: Path, relative_path: str) -> Path:
    root = volume_root.resolve()
    candidate = (root / relative_path).resolve()
    candidate.relative_to(root)
    return candidate


def decide_media_access(
    asset: LocalAsset | None,
    volume: StorageVolume | None,
) -> MediaAccessDecision:
    if asset is None:
        return MediaAccessDecision("not_found")
    if asset.lifecycle_state == "deleted":
        return MediaAccessDecision("deleted")
    if asset.lifecycle_state != "present" or asset.verification_state != "verified":
        return MediaAccessDecision("temporarily_unavailable")
    if volume is None or not volume.online:
        return MediaAccessDecision("temporarily_unavailable")

    try:
        path = resolve_media_path(Path(volume.mount_path_runtime), asset.relative_path)
    except (OSError, ValueError):
        return MediaAccessDecision("temporarily_unavailable")

    if not path.is_file():
        return MediaAccessDecision("temporarily_unavailable")
    return MediaAccessDecision("available", path)
