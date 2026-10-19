from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from pathlib import Path

from app.domain.thumbnails import ThumbnailError, generate_thumbnail, thumbnail_path


def _audit(root: Path, asset_ids: set[str]) -> tuple[list[str], list[str], list[str]]:
    thumb_root = root / "recordings" / "thumbnails"
    files = sorted(path for path in thumb_root.glob("*.jpg") if path.stem in asset_ids)
    missing = sorted(asset_ids - {path.stem for path in files})
    if not files:
        return missing, [], []
    process = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mjpeg", "-i", "-",
            "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-",
        ],
        input=b"".join(path.read_bytes() for path in files),
        capture_output=True,
        timeout=120,
        check=False,
    )
    text = process.stdout.decode(errors="replace") + process.stderr.decode(errors="replace")
    blocks = re.split(r"(?=frame:\s*\d+)", text)
    gray: list[str] = []
    undecodable: list[str] = []
    seen = 0
    for block in blocks:
        match = re.search(r"frame:\s*(\d+)", block)
        if not match:
            continue
        index = int(match.group(1))
        if index >= len(files):
            continue
        seen += 1
        values: dict[str, float] = {}
        for key in ("YLOW", "YHIGH", "SATAVG"):
            value = re.search(rf"lavfi\.signalstats\.{key}=([0-9.]+)", block)
            if value:
                values[key] = float(value.group(1))
        if values.get("YHIGH", 0) - values.get("YLOW", 999) <= 3 and values.get("SATAVG", 999) <= 1:
            gray.append(files[index].stem)
    if process.returncode != 0 or seen != len(files):
        decoded = {files[index].stem for index in range(min(seen, len(files)))}
        undecodable.extend(sorted(path.stem for path in files if path.stem not in decoded and path.stem not in gray))
    return missing, sorted(set(gray)), undecodable


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and repair only archived sidecars with normalized media")
    parser.add_argument("--database", default="/state/home-vision.db")
    parser.add_argument("--archive-root", default="/archive/a")
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    asset_ids = {
        row[0]
        for row in connection.execute(
            "select id from local_assets where asset_role='source_archive' and verification_state='verified' and lifecycle_state='present'"
        )
    }
    root = Path(args.archive_root)
    missing, gray, undecodable = _audit(root, asset_ids)
    report: dict[str, object] = {
        "asset_count": len(asset_ids),
        "sidecar_missing": len(missing),
        "sidecar_gray": len(gray),
        "sidecar_undecodable": len(undecodable),
        "repaired": 0,
        "no_normalized": [],
        "repair_failed": [],
    }
    if args.repair:
        for asset_id in gray:
            normalized = root / "recordings" / "normalized" / f"{asset_id}.mp4"
            if not normalized.is_file():
                report["no_normalized"].append(asset_id)
                continue
            try:
                result = generate_thumbnail(
                    normalized,
                    thumbnail_path(root, asset_id),
                    overwrite=True,
                    reject_uniform=True,
                )
                if result == "created":
                    report["repaired"] += 1
            except (ThumbnailError, OSError, subprocess.SubprocessError) as exc:
                report["repair_failed"].append({"asset_id": asset_id, "error": getattr(exc, "code", type(exc).__name__)})
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
