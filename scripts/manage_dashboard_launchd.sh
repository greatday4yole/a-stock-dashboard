#!/bin/bash
# Idempotent macOS LaunchAgent management for the local dashboard.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
LABEL="com.roglu.ldlchinastock.dashboard"
PORT="8765"
REQUIRED_API_CAPABILITIES="stock-chart.api.v1 daily-pnl-history.api.v1"
# 默认仍是上面的 8765 主实例字面量；并存部署第二实例时用环境变量覆盖，例如：
#   DASHBOARD_LAUNCHD_LABEL=com.roglu.ldlchinastock.dashboard-v11 \
#   DASHBOARD_LAUNCHD_PORT=8766 \
#   DASHBOARD_LAUNCHD_LOG_DIR="$HOME/Library/Logs/LDLChinaStock-V1.1" \
#   ./scripts/manage_dashboard_launchd.sh install
LABEL="${DASHBOARD_LAUNCHD_LABEL:-$LABEL}"
PORT="${DASHBOARD_LAUNCHD_PORT:-$PORT}"
REQUIRED_API_CAPABILITIES="${DASHBOARD_LAUNCHD_REQUIRED_CAPABILITIES:-$REQUIRED_API_CAPABILITIES}"
HEALTH_CHECK_MAX_ATTEMPTS="60"
HEALTH_CHECK_RETRY_SECONDS="0.5"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
PLIST_RENDERER="$PROJECT_ROOT/scripts/render_dashboard_launchd_plist.py"
SUPERVISOR="$PROJECT_ROOT/scripts/launchd_dashboard_supervisor.py"
DASHBOARD_SERVER="$PROJECT_ROOT/scripts/dashboard_server.py"
USER_ID="$(/usr/bin/id -u)"
DOMAIN="gui/$USER_ID"
SERVICE_TARGET="$DOMAIN/$LABEL"
USER_HOME_DIR="${HOME:?HOME is required}"
PLIST_DIR="$USER_HOME_DIR/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$USER_HOME_DIR/Library/Logs/LDLChinaStock"
LOG_DIR="${DASHBOARD_LAUNCHD_LOG_DIR:-$LOG_DIR}"
LOG_PATH="$LOG_DIR/dashboard.log"
NODE_CA_CERT_PATH="${NODE_EXTRA_CA_CERTS:-}"
if [ -z "$NODE_CA_CERT_PATH" ] && [ -r "$USER_HOME_DIR/combined_cert.pem" ]; then
  NODE_CA_CERT_PATH="$USER_HOME_DIR/combined_cert.pem"
fi

usage() {
  /bin/echo "Usage: $0 {preflight|render|install|start|status|stop|restart|uninstall|logs}"
  /bin/echo "  preflight  verify macOS, .venv, dependencies, paths and plist syntax"
  /bin/echo "  render     print the generated plist without installing it"
  /bin/echo "  install    atomically install and load the LaunchAgent (RunAtLoad)"
  /bin/echo "  start      load an already-installed LaunchAgent"
  /bin/echo "  status     show install/load/health state (nonzero unless healthy)"
  /bin/echo "  stop       gracefully unload but retain the installed plist"
  /bin/echo "  restart    gracefully unload, then load the installed plist"
  /bin/echo "  uninstall  gracefully unload and remove only the installed plist"
  /bin/echo "  logs       show bounded log paths and recent dashboard log lines"
}

die() {
  /bin/echo "ERROR: $*" >&2
  exit 1
}

is_loaded() {
  /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1
}

service_pid() {
  /bin/launchctl print "$SERVICE_TARGET" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/ { print $3; exit }'
}

is_descendant_of() {
  local candidate="$1"
  local ancestor="$2"
  local parent=""
  local loops=0
  while [ "$candidate" -gt 1 ] 2>/dev/null && [ "$loops" -lt 32 ]; do
    if [ "$candidate" = "$ancestor" ]; then
      return 0
    fi
    parent="$(/bin/ps -o ppid= -p "$candidate" 2>/dev/null | /usr/bin/tr -d '[:space:]')"
    [ -n "$parent" ] || return 1
    candidate="$parent"
    loops=$((loops + 1))
  done
  return 1
}

listener_pids() {
  /usr/sbin/lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null \
    | /usr/bin/sort -u || true
}

service_owns_listener() {
  local listeners=""
  local launchd_pid=""
  local listener=""
  is_loaded || return 1
  launchd_pid="$(service_pid)"
  [ -n "$launchd_pid" ] || return 1
  listeners="$(listener_pids)"
  [ -n "$listeners" ] || return 1
  while IFS= read -r listener; do
    [ -n "$listener" ] || continue
    is_descendant_of "$listener" "$launchd_pid" || return 1
  done <<< "$listeners"
  return 0
}

assert_port_safe_to_start() {
  local listeners=""
  local launchd_pid=""
  local listener=""
  listeners="$(listener_pids)"
  [ -n "$listeners" ] || return 0

  if is_loaded; then
    launchd_pid="$(service_pid)"
    if [ -n "$launchd_pid" ]; then
      while IFS= read -r listener; do
        if [ -n "$listener" ] && is_descendant_of "$listener" "$launchd_pid"; then
          continue
        fi
        die "TCP 127.0.0.1:$PORT is occupied by PID $listener, not this LaunchAgent; refusing to replace it"
      done <<< "$listeners"
      return 0
    fi
  fi

  die "TCP 127.0.0.1:$PORT is already occupied by PID(s): $(/bin/echo "$listeners" | /usr/bin/tr '\n' ' '). Stop it explicitly before loading launchd."
}

render_to() {
  local output="$1"
  local renderer_args=(
    --repo-root "$PROJECT_ROOT" \
    --python "$PYTHON_BIN" \
    --log-directory "$LOG_DIR" \
    --label "$LABEL" \
    --port "$PORT" \
    --output "$output"
  )
  if [ -n "$NODE_CA_CERT_PATH" ]; then
    renderer_args+=(--node-extra-ca-certs "$NODE_CA_CERT_PATH")
  fi
  "$PYTHON_BIN" "$PLIST_RENDERER" "${renderer_args[@]}"
}

preflight() {
  local python_version=""
  [ "$(/usr/bin/uname -s)" = "Darwin" ] || die "launchd setup is supported only on macOS"
  [ "$PROJECT_ROOT" = "${PROJECT_ROOT#/}" ] && die "project path must be absolute"
  [ "$USER_HOME_DIR" = "${USER_HOME_DIR#/}" ] && die "HOME must be absolute"
  [ -x /bin/launchctl ] || die "/bin/launchctl is unavailable"
  [ -x /usr/bin/plutil ] || die "/usr/bin/plutil is unavailable"
  [ -x /usr/sbin/lsof ] || die "/usr/sbin/lsof is unavailable; safe port ownership checks cannot run"
  [ -x "$PYTHON_BIN" ] || die "missing project Python: $PYTHON_BIN (create .venv and install requirements first)"
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(not ((3, 9) <= sys.version_info[:2] < (3, 13)))' >/dev/null 2>&1 \
    || die "$PYTHON_BIN must be Python 3.9 through 3.12"
  [ -f "$PLIST_RENDERER" ] || die "missing plist renderer: $PLIST_RENDERER"
  [ -f "$SUPERVISOR" ] || die "missing launchd supervisor: $SUPERVISOR"
  [ -f "$DASHBOARD_SERVER" ] || die "missing dashboard server: $DASHBOARD_SERVER"
  [ -f "$PROJECT_ROOT/dashboard/index.html" ] || die "missing dashboard/index.html"
  [ -f "$PROJECT_ROOT/config/portfolio.json" ] || die "missing config/portfolio.json"
  if [ -n "$NODE_CA_CERT_PATH" ]; then
    [ "$NODE_CA_CERT_PATH" != "${NODE_CA_CERT_PATH#/}" ] \
      || die "NODE_EXTRA_CA_CERTS must be an absolute path"
    [ -r "$NODE_CA_CERT_PATH" ] || die "NODE_EXTRA_CA_CERTS is not readable: $NODE_CA_CERT_PATH"
    ! /usr/bin/grep -Eq 'BEGIN .*PRIVATE KEY' "$NODE_CA_CERT_PATH" \
      || die "NODE_EXTRA_CA_CERTS must not contain a private key"
  fi
  "$PYTHON_BIN" -c 'import requests, chinese_calendar' >/dev/null 2>&1 \
    || die "$PYTHON_BIN cannot import requests and chinese_calendar; install the project Python requirements"

  local temporary=""
  temporary="$(/usr/bin/mktemp "${TMPDIR:-/tmp}/ldl-dashboard-plist.XXXXXX")"
  render_to "$temporary"
  if ! /usr/bin/plutil -lint "$temporary" >/dev/null; then
    /bin/rm -f "$temporary"
    die "generated plist failed plutil validation"
  fi
  /bin/rm -f "$temporary"
  python_version="$("$PYTHON_BIN" --version 2>&1)"
  /bin/echo "Preflight OK"
  /bin/echo "  Python: $PYTHON_BIN ($python_version)"
  /bin/echo "  WorkingDirectory: $PROJECT_ROOT"
  /bin/echo "  Listener: 127.0.0.1:$PORT"
  [ -z "$NODE_CA_CERT_PATH" ] || /bin/echo "  Node CA bundle: $NODE_CA_CERT_PATH"
  /bin/echo "  Plist: $PLIST_PATH"
  /bin/echo "  Rotating log: $LOG_PATH (5 MiB x current + 3 backups)"
}

wait_for_health() {
  local attempts=0
  local health_payload=""
  while [ "$attempts" -lt "$HEALTH_CHECK_MAX_ATTEMPTS" ]; do
    if service_owns_listener; then
      if health_payload="$(fetch_health_payload 2 2>/dev/null)" \
        && health_payload_is_compatible <<< "$health_payload"; then
        return 0
      fi
    fi
    /bin/sleep "$HEALTH_CHECK_RETRY_SECONDS"
    attempts=$((attempts + 1))
  done
  return 1
}

fetch_health_payload() {
  local timeout_seconds="$1"
  /usr/bin/curl -fsS --max-time "$timeout_seconds" \
    "http://127.0.0.1:$PORT/api/health"
}

health_payload_is_compatible() {
  "$PYTHON_BIN" -c '
import json
import sys

required_capabilities = sys.argv[1:]
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError, UnicodeError):
    raise SystemExit(1)

runtime = payload.get("runtime") if isinstance(payload, dict) else None
capabilities = runtime.get("api_capabilities") if isinstance(runtime, dict) else None
compatible = (
    isinstance(runtime, dict)
    and runtime.get("source_in_sync") is True
    and isinstance(capabilities, list)
    and all(capability in capabilities for capability in required_capabilities)
)
raise SystemExit(0 if compatible else 1)
' $REQUIRED_API_CAPABILITIES >/dev/null 2>&1
}

wait_for_pid_exit() {
  local old_pid="$1"
  local attempts=0
  [ -n "$old_pid" ] || return 0
  while /bin/kill -0 "$old_pid" 2>/dev/null; do
    [ "$attempts" -lt 40 ] || return 1
    /bin/sleep 0.5
    attempts=$((attempts + 1))
  done
  return 0
}

load_installed() {
  [ -f "$PLIST_PATH" ] || die "LaunchAgent is not installed: $PLIST_PATH"
  if is_loaded; then
    /bin/echo "LaunchAgent is already loaded; no change."
    wait_for_health || die "LaunchAgent is loaded but /api/health is not healthy; inspect $LOG_PATH"
    return 0
  fi
  assert_port_safe_to_start
  /bin/launchctl enable "$SERVICE_TARGET"
  /bin/launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
  if wait_for_health; then
    /bin/echo "LaunchAgent loaded and healthy: http://127.0.0.1:$PORT"
  else
    die "LaunchAgent loaded but health check timed out; inspect $LOG_PATH"
  fi
}

stop_loaded() {
  local old_pid=""
  if ! is_loaded; then
    /bin/echo "LaunchAgent is already stopped; no change."
    return 0
  fi
  old_pid="$(service_pid)"
  /bin/echo "Requesting graceful unload (TERM to supervisor, INT to dashboard)..."
  /bin/launchctl bootout "$SERVICE_TARGET"
  if wait_for_pid_exit "$old_pid"; then
    /bin/echo "LaunchAgent stopped cleanly."
  else
    die "launchd unloaded the job but supervisor PID $old_pid did not exit within 20 seconds"
  fi
}

install_agent() {
  preflight
  /bin/mkdir -p "$PLIST_DIR" "$LOG_DIR"
  /bin/chmod 700 "$LOG_DIR"

  local temporary=""
  local installed_same="false"
  temporary="$(/usr/bin/mktemp "${TMPDIR:-/tmp}/ldl-dashboard-plist.XXXXXX")"
  render_to "$temporary"
  /usr/bin/plutil -lint "$temporary" >/dev/null
  if [ -f "$PLIST_PATH" ] && /usr/bin/cmp -s "$temporary" "$PLIST_PATH"; then
    installed_same="true"
  fi

  if [ "$installed_same" = "true" ] && is_loaded; then
    /bin/rm -f "$temporary"
    /bin/echo "LaunchAgent is already installed with identical configuration; no change."
    wait_for_health || die "installed LaunchAgent is loaded but unhealthy; inspect $LOG_PATH"
    return 0
  fi

  if is_loaded; then
    stop_loaded
  fi
  assert_port_safe_to_start
  /usr/bin/install -m 0644 "$temporary" "$PLIST_PATH.tmp.$$"
  /bin/mv -f "$PLIST_PATH.tmp.$$" "$PLIST_PATH"
  /bin/rm -f "$temporary"
  load_installed
}

show_status() {
  local installed="no"
  local loaded="no"
  local pid="-"
  local endpoint="no"
  local runtime_compatible="no"
  local healthy="no"
  local health_payload=""
  [ -f "$PLIST_PATH" ] && installed="yes"
  if is_loaded; then
    loaded="yes"
    pid="$(service_pid)"
    [ -n "$pid" ] || pid="starting"
  fi
  if health_payload="$(fetch_health_payload 3 2>/dev/null)"; then
    endpoint="yes"
    if health_payload_is_compatible <<< "$health_payload"; then
      runtime_compatible="yes"
    fi
    if [ "$runtime_compatible" = "yes" ] && service_owns_listener; then
      healthy="yes"
    fi
  fi
  /bin/echo "LaunchAgent status"
  /bin/echo "  installed: $installed ($PLIST_PATH)"
  /bin/echo "  loaded:    $loaded ($SERVICE_TARGET)"
  /bin/echo "  pid:       $pid"
  /bin/echo "  endpoint:  $endpoint (http://127.0.0.1:$PORT/api/health)"
  /bin/echo "  runtime:   $runtime_compatible (source in sync; capabilities $REQUIRED_API_CAPABILITIES)"
  /bin/echo "  healthy:   $healthy (owned endpoint with compatible runtime)"
  /bin/echo "  log:       $LOG_PATH"
  [ "$installed" = "yes" ] && [ "$loaded" = "yes" ] && [ "$healthy" = "yes" ]
}

uninstall_agent() {
  stop_loaded
  if [ -f "$PLIST_PATH" ]; then
    /bin/rm -f "$PLIST_PATH"
    /bin/echo "Removed $PLIST_PATH"
  else
    /bin/echo "LaunchAgent plist is already absent; no change."
  fi
  /bin/echo "Logs were retained at $LOG_DIR (use an explicit file deletion if no longer needed)."
}

show_logs() {
  /bin/echo "Rotating log: $LOG_PATH"
  /bin/echo "Backups: $LOG_PATH.1 through $LOG_PATH.3"
  if [ -f "$LOG_PATH" ]; then
    /usr/bin/tail -n 80 "$LOG_PATH"
  else
    /bin/echo "No dashboard log has been created yet."
  fi
}

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
fi

command="${1:-}"
case "$command" in
  preflight)
    preflight
    ;;
  render)
    preflight >/dev/null
    render_to "-"
    ;;
  install)
    install_agent
    ;;
  start)
    preflight
    load_installed
    ;;
  status)
    show_status
    ;;
  stop)
    stop_loaded
    ;;
  restart)
    preflight
    stop_loaded
    load_installed
    ;;
  uninstall)
    uninstall_agent
    ;;
  logs)
    show_logs
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac
