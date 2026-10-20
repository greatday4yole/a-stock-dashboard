#!/bin/bash
# Run the pinned HiThink CLI with the project-private, supported Node runtime.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
NODE_BIN="$PROJECT_ROOT/data/runtime/node/bin/node"
CLI_ENTRY="$PROJECT_ROOT/node_modules/@hithink-tech/hithink-finance-cli/dist/cli/main.js"

[ -x "$NODE_BIN" ] || {
  /bin/echo "Project Node runtime is missing. Run: /bin/bash scripts/install_project_node_runtime.sh" >&2
  exit 69
}
[ -f "$CLI_ENTRY" ] || {
  /bin/echo "Pinned hithink-finance CLI is missing. Run: pnpm install --frozen-lockfile" >&2
  exit 69
}

exec "$NODE_BIN" "$CLI_ENTRY" "$@"
