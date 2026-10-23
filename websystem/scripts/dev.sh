#!/usr/bin/env bash
# Start (or stop) the whole system locally.
#
#   ./scripts/dev.sh          start everything, tail the logs
#   ./scripts/dev.sh stop     stop everything
#   ./scripts/dev.sh status   what is running
#
# Three processes, mirroring the Pi deployment: the MQTT broker, the inference
# worker that owns the camera, and the Flask dashboard.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv/bin/python"
MOSQUITTO="${PW_MOSQUITTO:-/opt/homebrew/opt/mosquitto/sbin/mosquitto}"
PORT="${PW_WEB_PORT:-8000}"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }

stop() {
  log "Stopping"
  pkill -f "pepperwatch.worker" 2>/dev/null || true
  pkill -f "pepperwatch.web" 2>/dev/null || true
  pkill -f "mosquitto -c var/mosquitto" 2>/dev/null || true
  sleep 1
  printf '    stopped\n'
}

status() {
  pgrep -fl "mosquitto -c var/mosquitto|pepperwatch.worker|pepperwatch.web" \
    | sed 's/^/    /' || printf '    nothing running\n'
}

case "${1:-start}" in
  stop)   stop; exit 0 ;;
  status) status; exit 0 ;;
  start)  ;;
  *)      echo "usage: $0 [start|stop|status]" >&2; exit 1 ;;
esac

[ -x "$VENV" ] || { echo "no venv — run: python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2; exit 1; }

# The mock camera replays the Naawan photographs; without the cache there is
# nothing to show.
if [ ! -d var/mock_frames/preview ] || [ -z "$(ls -A var/mock_frames/preview 2>/dev/null)" ]; then
  log "Building the mock frame cache (first run only, ~1 min)"
  "$VENV" scripts/prepare_mock_frames.py
fi

mkdir -p var/mosquitto

# var/ is gitignored, so a fresh clone has no broker config yet.
if [ ! -f var/mosquitto/dev.conf ]; then
  cat > var/mosquitto/dev.conf <<'CONF'
# Development broker config, mirroring what deploy/mosquitto ships to the Pi.
listener 1883 127.0.0.1
allow_anonymous true
persistence false
CONF
fi

if ! pgrep -f "mosquitto -c var/mosquitto" >/dev/null; then
  if [ ! -x "$MOSQUITTO" ]; then
    echo "mosquitto not found at $MOSQUITTO — install with: brew install mosquitto" >&2
    echo "(or set PW_MOSQUITTO to its path)" >&2
    exit 1
  fi
  log "Starting the MQTT broker"
  "$MOSQUITTO" -c var/mosquitto/dev.conf > var/mosquitto/dev.log 2>&1 &
  sleep 1
fi

pkill -f "pepperwatch.worker" 2>/dev/null || true
pkill -f "pepperwatch.web" 2>/dev/null || true
sleep 1

log "Starting the inference worker"
nohup "$VENV" -m pepperwatch.worker > var/worker.log 2>&1 &
sleep 3

log "Starting the dashboard"
nohup "$VENV" -m pepperwatch.web > var/web.log 2>&1 &

# Wait for it to answer rather than guessing at a sleep duration.
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

if ! curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
  echo "the dashboard did not come up; last lines of var/web.log:" >&2
  tail -20 var/web.log >&2
  exit 1
fi

printf '\n'
log "Running"
status
printf '\n'
printf '    dashboard   http://localhost:%s\n' "$PORT"
printf '    logs        tail -f var/worker.log var/web.log\n'
printf '    stop        ./scripts/dev.sh stop\n'
printf '\n'
printf '    Click "Start Camera" on the page — the feed is off by default,\n'
printf '    matching the Figure 3.4 wireframe.\n'
printf '\n'

if [ "${PW_NO_TAIL:-}" = "" ]; then
  log "Tailing logs (Ctrl-C to detach; the system keeps running)"
  tail -f var/worker.log var/web.log
fi
