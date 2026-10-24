#!/usr/bin/env bash
# Thin loopback client for the persistent Tokyo host runtime-agent.
set -euo pipefail

BASE="${P20_RUNTIME_AGENT_URL:-http://127.0.0.1:17613}"
TOKEN_FILE="${P20_RUNTIME_AGENT_TOKEN_FILE:-/home/ubuntu/p20-transfer/runtime-agent.token}"
kind="${1:-}"
action="${2:-status}"

case "$kind:$action" in
  qq:status) method=GET; path=/status ;;
  qq:start) method=POST; path=/start ;;
  qq:stop) method=POST; path=/stop ;;
  qq:autostop) method=POST; path=/autostop ;;
  browser:status) method=GET; path=/browser/status ;;
  browser:start) method=POST; path=/browser/start ;;
  browser:stop) method=POST; path=/browser/stop ;;
  hub:status) method=GET; path=/hub/status ;;
  hub:start) method=POST; path=/hub/start ;;
  hub:stop-if-idle) method=POST; path=/hub/stop-if-idle ;;
  *) echo "usage: $0 {qq|browser|hub} action" >&2; exit 64 ;;
esac

token=$(cat "$TOKEN_FILE")
# Feed the Authorization header over stdin so bearer credentials never appear
# in process argv, systemctl status, or journal command lines.
printf 'header = "Authorization: Bearer %s"\n' "$token" \
  | curl -fsS --max-time 220 -X "$method" --config - "$BASE$path"
