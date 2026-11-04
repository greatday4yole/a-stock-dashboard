#!/usr/bin/env bash
# Sync the deployable code and rebuild the encrypted runtime data bundle.
#
# Usage:
#   export SOURCE_DATA_KEY='...'
#   ./build/sync_from_project.sh /path/to/LDLChinaStock-V1.1
#   ./build/sync_from_project.sh --code-only /path/to/LDLChinaStock-V1.1
set -euo pipefail

CODE_ONLY=0
if [[ "${1:-}" == "--code-only" ]]; then
  CODE_ONLY=1
  shift
fi

PROJECT="${1:-../LDLChinaStock-V1.1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$(cd "$PROJECT" && pwd)"

if [[ "$CODE_ONLY" -eq 0 && -z "${SOURCE_DATA_KEY:-}" ]]; then
  echo "SOURCE_DATA_KEY is not set (or use --code-only to preserve the encrypted bundle)" >&2
  exit 2
fi

echo "[sync] project: $PROJECT"
echo "[sync] copying scripts/"
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$PROJECT/scripts/" "$ROOT/scripts/"

echo "[sync] copying static frontend"
cp "$PROJECT/dashboard/index.html" \
   "$PROJECT/dashboard/styles.css" \
   "$PROJECT/dashboard/app.js" \
   "$PROJECT/dashboard/autopilot.html" \
   "$PROJECT/dashboard/autopilot.js" \
   "$ROOT/dashboard/"
mkdir -p "$ROOT/dashboard/vendor"
cp "$PROJECT/dashboard/vendor/echarts.min.js" "$ROOT/dashboard/vendor/"

echo "[sync] copying desktop widget sources"
cp "$PROJECT/dashboard/widget.html" \
   "$PROJECT/dashboard/widget.css" \
   "$PROJECT/dashboard/widget.js" \
   "$ROOT/dashboard/"
mkdir -p "$ROOT/tests"
cp "$PROJECT/tests/desktop_widget.test.mjs" "$ROOT/tests/"

if [[ "$CODE_ONLY" -eq 1 ]]; then
  echo "[sync] code-only: preserving encrypted data bundle"
else
  echo "[sync] rebuilding encrypted data bundle"
  python3 "$ROOT/build/prepare_bundle.py" \
    --project "$PROJECT" \
    --out "$ROOT/bundle" \
    --history-days "${INTRADAY_HISTORY_DAYS:-10}"
fi

echo "[sync] done. Review and push:"
echo "  git add -A && git commit -m 'refresh dashboard snapshot' && git push"
