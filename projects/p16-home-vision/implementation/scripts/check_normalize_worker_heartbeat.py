from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    try:
        payload = json.loads(Path("/state/normalize-worker-heartbeat.json").read_text(encoding="utf-8"))
        heartbeat_at = datetime.fromisoformat(str(payload["heartbeat_at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 1
    return 0 if age <= 90 else 1


if __name__ == "__main__":
    sys.exit(main())
