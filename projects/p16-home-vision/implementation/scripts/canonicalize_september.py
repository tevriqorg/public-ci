#!/usr/bin/env python3
"""Plan or apply September duplicate source canonicalization.

Default mode is read-only. Applying requires an explicit SQLite backup path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal
from app.domain.source_canonicalization import canonicalize_sources


def _db_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise RuntimeError("BACKUP_ONLY_SUPPORTS_SQLITE")
    return Path(url.removeprefix("sqlite:///"))


def _backup(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as backup_db:
        source_db.backup(backup_db)
    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default="2026-09")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-conflicts", action="store_true", help="apply only proven-safe groups and leave conflicts unchanged")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error("--apply requires --backup")
    backup_sha256 = None
    if args.apply:
        backup_sha256 = _backup(_db_path(), args.backup)
    with SessionLocal() as session:
        result = canonicalize_sources(session, month=args.month, apply=args.apply, allow_conflicts=args.allow_conflicts)
        if args.apply:
            session.commit()
        print(json.dumps({
            "month": result.month,
            "applied": result.applied,
            "backup_sha256": backup_sha256,
            "groups": [group.__dict__ for group in result.groups],
        }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
