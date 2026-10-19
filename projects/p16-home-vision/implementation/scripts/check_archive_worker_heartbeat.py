from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from app.config import get_settings


def main() -> int:
    try:
        payload = json.loads(get_settings().archive_heartbeat_path.read_text(encoding="utf-8"))
        heartbeat_at = datetime.fromisoformat(str(payload["heartbeat_at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 1
    return 0 if age <= 30 else 1


if __name__ == "__main__":
    sys.exit(main())
