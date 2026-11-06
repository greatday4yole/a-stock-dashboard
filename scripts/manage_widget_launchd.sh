#!/bin/bash
# Idempotent macOS LaunchAgent management for the desktop P&L widget.
#
# The widget is a GUI Electron app, so it belongs to the user's Aqua session.
# KeepAlive keeps it running; the app itself hides outside A-share hours.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
LABEL="com.roglu.ldlchinastock.widget"
ELECTRON_BIN="$PROJECT_ROOT/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
WIDGET_MAIN="$PROJECT_ROOT/scripts/desktop_widget.cjs"
NATIVE_MODULE="$PROJECT_ROOT/data/runtime/desktop_window_level.node"
ORIGIN="${DASHBOARD_ORIGIN:-http://127.0.0.1:8766}"
USER_ID="$(/usr/bin/id -u)"
DOMAIN="gui/$USER_ID"
SERVICE_TARGET="$DOMAIN/$LABEL"
USER_HOME_DIR="${HOME:?HOME is required}"
PLIST_DIR="$USER_HOME_DIR/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$USER_HOME_DIR/Library/Logs/LDLChinaStock"
LOG_PATH="$LOG_DIR/widget.log"
ERROR_LOG_PATH="$LOG_DIR/widget.error.log"
TEMP_PLIST="$(/usr/bin/mktemp -t widget-launchd.XXXXXX)"
trap '/bin/rm -f "$TEMP_PLIST"' EXIT

usage() {
  /bin/echo "Usage: $0 {preflight|render|install|start|status|stop|restart|uninstall|logs}"
  /bin/echo "  preflight  verify paths, origin and generated plist syntax"
  /bin/echo "  render     print the generated plist without installing it"
  /bin/echo "  install    atomically install and bootstrap the LaunchAgent"
  /bin/echo "  start      load an installed agent, or kickstart a running one"
  /bin/echo "  status     show load/run state and recent log paths"
  /bin/echo "  stop       gracefully unload but retain the installed plist"
  /bin/echo "  restart    kickstart the running agent (kill + relaunch)"
  /bin/echo "  uninstall  unload and remove only the widget plist"
  /bin/echo "  logs       show the bounded tail of widget stdout/stderr"
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

render_plist() {
  /bin/cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$ELECTRON_BIN</string>
		<string>$WIDGET_MAIN</string>
	</array>
	<key>WorkingDirectory</key>
	<string>$PROJECT_ROOT</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>ThrottleInterval</key>
	<integer>15</integer>
	<key>StandardOutPath</key>
	<string>$LOG_PATH</string>
	<key>StandardErrorPath</key>
	<string>$ERROR_LOG_PATH</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>DASHBOARD_ORIGIN</key>
		<string>$ORIGIN</string>
		<key>LC_CTYPE</key>
		<string>UTF-8</string>
	</dict>
</dict>
</plist>
PLIST
}

preflight() {
  [[ "$(/usr/bin/uname -s)" == "Darwin" ]] || die "this LaunchAgent is macOS-only"
  [[ -x "$ELECTRON_BIN" ]] || die "Electron binary is missing: $ELECTRON_BIN"
  [[ -f "$WIDGET_MAIN" ]] || die "widget entrypoint is missing: $WIDGET_MAIN"
  [[ -f "$NATIVE_MODULE" ]] || die "native window-level module is missing: $NATIVE_MODULE (run pnpm run widget:build)"
  [[ "$ORIGIN" =~ ^http://(127\.0\.0\.1|localhost):[0-9]{1,5}$ ]] \
    || die "DASHBOARD_ORIGIN must be a loopback HTTP origin, got: $ORIGIN"
  render_plist > "$TEMP_PLIST"
  /usr/bin/plutil -lint "$TEMP_PLIST" >/dev/null || die "generated plist failed plutil -lint"
  /bin/echo "preflight ok"
  /bin/echo "  label:    $LABEL"
  /bin/echo "  electron: $ELECTRON_BIN"
  /bin/echo "  widget:   $WIDGET_MAIN"
  /bin/echo "  origin:   $ORIGIN"
  /bin/echo "  plist:    $PLIST_PATH"
}

install_agent() {
  preflight
  /bin/mkdir -p "$PLIST_DIR" "$LOG_DIR"
  render_plist > "$TEMP_PLIST"
  /usr/bin/plutil -lint "$TEMP_PLIST" >/dev/null
  /bin/cp "$TEMP_PLIST" "$PLIST_PATH.tmp"
  /bin/chmod 0644 "$PLIST_PATH.tmp"
  /bin/mv -f "$PLIST_PATH.tmp" "$PLIST_PATH"
  if is_loaded; then
    /bin/launchctl bootout "$SERVICE_TARGET" >/dev/null 2>&1 || true
  fi
  /bin/launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
  /bin/launchctl enable "$SERVICE_TARGET" >/dev/null 2>&1 || true
  /bin/sleep 2
  status
}

start_agent() {
  [[ -f "$PLIST_PATH" ]] || die "LaunchAgent is not installed; run: $0 install"
  if is_loaded; then
    /bin/launchctl kickstart -k "$SERVICE_TARGET"
  else
    /bin/launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
  fi
  /bin/sleep 2
  status
}

stop_agent() {
  if is_loaded; then
    /bin/launchctl bootout "$SERVICE_TARGET"
    /bin/echo "stopped $LABEL (plist retained at $PLIST_PATH)"
  else
    /bin/echo "$LABEL is not loaded"
  fi
}

status() {
  if is_loaded; then
    local pid
    pid="$(service_pid)"
    /bin/echo "loaded: yes"
    /bin/echo "state:  $(/bin/launchctl print "$SERVICE_TARGET" 2>/dev/null | /usr/bin/awk -F'= ' '/^[[:space:]]*state = / { print $2; exit }')"
    /bin/echo "pid:    ${pid:-unknown}"
    /bin/echo "origin: $ORIGIN"
  else
    /bin/echo "loaded: no"
  fi
  /bin/echo "plist:  $PLIST_PATH"
  /bin/echo "logs:   $LOG_PATH"
  /bin/echo "errors: $ERROR_LOG_PATH"
}

logs() {
  for file in "$LOG_PATH" "$ERROR_LOG_PATH"; do
    if [[ -f "$file" ]]; then
      /bin/echo "--- $file (tail 50) ---"
      /usr/bin/tail -n 50 "$file"
    fi
  done
}

uninstall_agent() {
  stop_agent
  /bin/rm -f "$PLIST_PATH"
  /bin/echo "removed $PLIST_PATH"
}

case "${1:-}" in
  preflight) preflight ;;
  render) render_plist ;;
  install) install_agent ;;
  start) start_agent ;;
  status) status ;;
  stop) stop_agent ;;
  restart) start_agent ;;
  uninstall) uninstall_agent ;;
  logs) logs ;;
  *) usage; exit 2 ;;
esac
