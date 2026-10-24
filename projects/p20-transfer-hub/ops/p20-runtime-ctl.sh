#!/usr/bin/env bash
# P20 QQ runtime lifecycle controller (Tokyo host).
#
# Starts / stops the qq-runtime compose stack on demand.
# NEVER removes containers and NEVER touches the login-state volumes:
# stopping is always `docker compose stop`, so the QQ profile (Cookies, device
# identity, LLBot session) survives and the next start auto-logs-in.
set -uo pipefail

REPO="${P20_REPO:-/home/ubuntu/work/P20-QQ-Runtime}"
COMPOSE_DIR="$REPO/deployments/tokyo/qq-runtime"
COMPOSE_FILE="$COMPOSE_DIR/compose.yaml"
ENV_FILE="$COMPOSE_DIR/.env"
QQ_RUNTIME_DIR="${QQ_RUNTIME_DIR:-/home/ubuntu/qqxfer-llbot}"
TRANSFER_DB="${P20_TRANSFER_DB:-/home/ubuntu/p20-transfer/transfer.db}"
STATE_DIR="${P20_OPS_DIR:-/home/ubuntu/p20-ops}"
LOG="$STATE_DIR/runtime-ctl.log"
STATE="$STATE_DIR/runtime-last-activity"

# QQ is needed only by qq_flash DELIVER work. Fetch/Process/READY must not keep it resident.
IDLE_MINUTES="${P20_RUNTIME_IDLE_MINUTES:-5}"

if [ "${P20_DOCKER_SUDO:-0}" = "1" ]; then
  DOCKER=(sudo docker)
  COMPOSE=(sudo /usr/libexec/docker/cli-plugins/docker-compose)
else
  DOCKER=(docker)
  COMPOSE=(docker compose)
fi

mkdir -p "$STATE_DIR" 2>/dev/null || true
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG" 2>/dev/null; echo "$*"; }
dc() { "${COMPOSE[@]}" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

running() {
  "${DOCKER[@]}" inspect -f '{{.State.Running}}' qqxfer-pmhq 2>/dev/null | grep -q true
}

# Logged in? Ask the OneBot HTTP API via the loopback port llbot publishes.
login_info() {
  local tok
  tok=$(cat "$QQ_RUNTIME_DIR/onebot.token" 2>/dev/null | tr -d '\n')
  [ -n "$tok" ] || { echo "unknown"; return; }
  printf 'header = "Authorization: Bearer %s"\n' "$tok" \
    | curl -s -m 6 --config - http://127.0.0.1:16200/get_login_info 2>/dev/null \
    | grep -q '"status":"ok"' && echo "yes" || echo "no"
}

active_jobs() {
  [ -f "$TRANSFER_DB" ] || { echo 0; return; }
  python3 - "$TRANSFER_DB" <<'PY2' 2>/dev/null || echo 0
import sqlite3, sys
db=sys.argv[1]
try:
    c=sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "job_stages" in tables:
        n=c.execute("""SELECT COUNT(*) FROM job_stages s JOIN jobs j ON j.id=s.job_id
          WHERE s.stage='DELIVER' AND s.state='RUNNING'
            AND COALESCE(json_extract(j.options_json,'$.delivery_backend'),'qq_flash')='qq_flash'""").fetchone()[0]
    else:
        n=c.execute("SELECT COUNT(*) FROM jobs WHERE state='FLASH_UPLOADING'").fetchone()[0]
    print(n)
except Exception:
    print(0)
PY2
}

# Newest QQ delivery activity timestamp plus manual runtime start.
last_activity() {
  local t=0
  if [ -f "$TRANSFER_DB" ]; then
    local jt
    jt=$(python3 - "$TRANSFER_DB" <<'PY' 2>/dev/null
import sqlite3, sys, datetime
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "job_stages" in tables:
        row=c.execute("""SELECT MAX(s.updated_at) FROM job_stages s JOIN jobs j ON j.id=s.job_id
          WHERE s.stage='DELIVER' AND COALESCE(json_extract(j.options_json,'$.delivery_backend'),'qq_flash')='qq_flash'""").fetchone()[0]
    else:
        row=c.execute("SELECT MAX(updated_at) FROM jobs WHERE state='FLASH_UPLOADING'").fetchone()[0]
    print(row or "")
except Exception:
    print("")
PY
)
    if [ -n "$jt" ]; then
      local e
      e=$(date -d "$jt" +%s 2>/dev/null || echo 0)
      [ "$e" -gt "$t" ] && t="$e"
    fi
  fi
  # A manual start resets the idle clock, so a fresh start is never auto-stopped
  # immediately because the last transfer happened hours ago.
  if [ -f "$STATE" ]; then
    local e
    e=$(stat -c %Y "$STATE" 2>/dev/null || echo 0)
    [ "$e" -gt "$t" ] && t="$e"
  fi
  echo "$t"
}

cmd_status() {
  local st="stopped" logn="?"
  if running; then st="running"; logn=$(login_info); fi
  local aj; aj=$(active_jobs)
  local la; la=$(last_activity)
  printf '{"state":"%s","logged_in":"%s","active_jobs":%s,"last_activity":%s,"idle_minutes":%s}\n' \
    "$st" "$logn" "$aj" "${la:-0}" "$IDLE_MINUTES"
}

cmd_start() {
  if running; then
    touch "$STATE"
    log "start: already running; idle lease refreshed"
    cmd_status
    return 0
  fi
  log "start: bringing up qq-runtime"
  dc up -d >/dev/null 2>&1
  # Wait until QQ is logged in (auto-login from the persisted profile).
  local i
  for i in $(seq 1 30); do
    if [ "$(login_info)" = "yes" ]; then
      log "start: logged in after ${i} polls"
      echo "$(date +%s)" >"$STATE"
      cmd_status
      return 0
    fi
    sleep 2
  done
  log "start: not logged in after timeout"
  cmd_status
  return 1
}

cmd_stop() {
  local force="${1:-}"
  if ! running; then
    log "stop: already stopped"
    return 0
  fi
  local aj; aj=$(active_jobs)
  if [ "$aj" != "0" ] && [ "$force" != "--force" ]; then
    log "stop: refused, $aj active transfer job(s)"
    return 2
  fi
  log "stop: stopping qq-runtime (volumes untouched)"
  dc stop >/dev/null 2>&1
  cmd_status
}

# Auto-stop only when idle long enough.
cmd_autostop() {
  running || { log "autostop: not running"; return 0; }
  local aj; aj=$(active_jobs)
  if [ "$aj" != "0" ]; then log "autostop: $aj active job(s), skip"; return 0; fi
  local la now idle_s
  la=$(last_activity); now=$(date +%s)
  idle_s=$(( now - ${la:-0} ))
  if [ "$idle_s" -ge $(( IDLE_MINUTES * 60 )) ]; then
    log "autostop: idle ${idle_s}s >= ${IDLE_MINUTES}m, stopping"
    cmd_stop
  else
    log "autostop: idle ${idle_s}s < $(( IDLE_MINUTES * 60 ))s, keep running"
  fi
}

case "${1:-status}" in
  status)   cmd_status ;;
  start)    cmd_start ;;
  stop)     shift; cmd_stop "${1:-}" ;;
  autostop) cmd_autostop ;;
  *) echo "usage: $0 {status|start|stop [--force]|autostop}" >&2; exit 64 ;;
esac
