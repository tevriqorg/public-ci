#!/bin/sh
set -eu

: "${HOME_VISION_DATABASE_URL:=sqlite:////state/home-vision.db}"
: "${HOME_VISION_DATA_ROOT:=/state/data}"

export HOME_VISION_DATABASE_URL HOME_VISION_DATA_ROOT

mkdir -p "${HOME_VISION_DATA_ROOT}"

# All P16 containers share the same SQLite state. Serialize migrations so a
# simultaneous worker/API startup cannot race on CREATE TABLE or alembic_version.
python - <<'PY'
import fcntl
from pathlib import Path
import subprocess

lock_path = Path("/state/.alembic-migration.lock")
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    subprocess.run(["alembic", "upgrade", "head"], check=True)
PY

exec "$@"
