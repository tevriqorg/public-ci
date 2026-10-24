#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${P20_TRANSFER_DATA:-/var/lib/p20-transfer}"
install -d -m 700 "$DATA_DIR" "$DATA_DIR/secrets" "$DATA_DIR/jobs" "$DATA_DIR/uploads"
if [[ ! -s "$DATA_DIR/api.token" ]]; then
  umask 077
  python3 - <<'PY' > "$DATA_DIR/api.token"
import secrets
print(secrets.token_urlsafe(32))
PY
  echo "Created $DATA_DIR/api.token"
else
  echo "Existing API token kept: $DATA_DIR/api.token"
fi
chmod 600 "$DATA_DIR/api.token"
