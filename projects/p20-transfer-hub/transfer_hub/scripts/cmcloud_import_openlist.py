#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from transfer_hub.cmcloud import _atomic_text, decode_authorization


def main() -> int:
    parser = argparse.ArgumentParser(description="Import one OpenList 139Yun Authorization into the P20 CMCloud credential file without printing it.")
    parser.add_argument("database", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--mount", default="/yd")
    args = parser.parse_args()

    con = sqlite3.connect(args.database)
    row = con.execute(
        "select addition from x_storages where driver=? and mount_path=?",
        ("139Yun", args.mount),
    ).fetchone()
    if not row:
        raise SystemExit("139Yun storage not found")
    raw = str(json.loads(row[0]).get("authorization") or "").strip()
    info = decode_authorization(raw)
    if info.prefix != "pc":
        raise SystemExit("unexpected CMCloud authorization type")
    _atomic_text(args.destination, raw + "\n", 0o600)
    print(f"imported account={info.account[:3]}****{info.account[-4:]} expires_at={info.expires_at} mode={oct(args.destination.stat().st_mode & 0o777)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
