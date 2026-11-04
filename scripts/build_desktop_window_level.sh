#!/bin/bash
# 构建仅负责 macOS 桌面窗口层级的 N-API 适配，不改变盈亏数据链路。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
if [[ "$(/usr/bin/uname -s)" != "Darwin" ]]; then
  exit 0
fi

NODE_BIN="${NODE_BIN:-node}"
NODE_INCLUDE="$($NODE_BIN -p 'require("node:path").join(require("node:path").dirname(process.execPath), "..", "include", "node")')"
if [[ ! -f "$NODE_INCLUDE/node_api.h" ]]; then
  printf '错误：找不到 Node N-API 头文件：%s\n' "$NODE_INCLUDE" >&2
  exit 1
fi

SOURCE="$SCRIPT_DIR/desktop_window_level.mm"
OUTPUT_DIR="$PROJECT_ROOT/data/runtime"
OUTPUT="$OUTPUT_DIR/desktop_window_level.node"
TEMP="$OUTPUT.tmp.$$"
trap '/bin/rm -f "$TEMP"' EXIT
/bin/mkdir -p "$OUTPUT_DIR"

/usr/bin/xcrun clang++ \
  -std=c++17 \
  -fobjc-arc \
  -fPIC \
  -bundle \
  -undefined dynamic_lookup \
  -I"$NODE_INCLUDE" \
  -framework Cocoa \
  -framework CoreGraphics \
  -o "$TEMP" \
  "$SOURCE"
/bin/mv -f "$TEMP" "$OUTPUT"
trap - EXIT
printf '已构建 macOS 桌面窗口层级适配：%s\n' "$OUTPUT"
