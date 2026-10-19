from __future__ import annotations

import argparse
import json
import logging
import time

from app.db import SessionLocal
from app.domain.archive_audit import audit_archive_state


LOGGER = logging.getLogger("home_vision.archive_reconcile")


def reconcile_once(*, include_files: bool = False) -> dict:
    with SessionLocal() as session:
        return audit_archive_state(session, include_file_orphans=include_files)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only P16 archive/coverage reconciliation")
    parser.add_argument("--include-files", action="store_true", help="walk recordings for orphan MP4/.part evidence")
    parser.add_argument("--watch", action="store_true", help="repeat read-only reconciliation")
    parser.add_argument("--interval-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.interval_seconds < 60:
        parser.error("--interval-seconds cannot be less than 60")

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    while True:
        report = reconcile_once(include_files=args.include_files)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        LOGGER.info(
            "archive reconciliation complete: findings=%s file_scan=%s",
            len(report["findings"]),
            report["file_orphan_scan"]["performed"],
        )
        if not args.watch:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
