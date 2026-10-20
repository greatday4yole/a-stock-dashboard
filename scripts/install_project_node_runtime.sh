#!/bin/bash
# Install a checksum-verified Node LTS runtime without changing the system Node.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
RUNTIME_ROOT="$PROJECT_ROOT/data/runtime"
TARGET="$RUNTIME_ROOT/node"
VERSION="v22.23.2"
NODE_DIST_URL="https://nodejs.org/dist/$VERSION"
MACHINE_ARCH="$(/usr/bin/uname -m)"
temporary=""

cleanup() {
  if [[ -n "$temporary" && "$temporary" == "$RUNTIME_ROOT"/.node-install.* && -d "$temporary" ]]; then
    /bin/rm -rf -- "$temporary"
  fi
}
trap cleanup EXIT INT TERM

case "$MACHINE_ARCH" in
  arm64)
    NODE_ARCH="darwin-arm64"
    # Keep the already audited Apple Silicon artifact fail-closed and pinned.
    PINNED_SHA256="5eff7a9011895aae3f29d06f167b84a62b028a591370c7cafb59103559fd26e1"
    ;;
  x86_64)
    NODE_ARCH="darwin-x64"
    # Intel support verifies the archive against the release checksum manifest.
    # No Intel archive or checksum is silently borrowed from another release.
    PINNED_SHA256=""
    ;;
  *)
    /bin/echo "Unsupported Mac architecture: $MACHINE_ARCH (expected arm64 or x86_64)." >&2
    exit 69
    ;;
esac

ARCHIVE="node-$VERSION-$NODE_ARCH.tar.xz"

[ "$(/usr/bin/uname -s)" = "Darwin" ] || {
  /bin/echo "This installer is pinned for macOS." >&2
  exit 69
}
if [ -x "$TARGET/bin/node" ]; then
  installed_version="$("$TARGET/bin/node" --version)"
  [ "$installed_version" = "$VERSION" ] || {
    /bin/echo "Refusing to replace existing project runtime $installed_version at $TARGET" >&2
    exit 73
  }
  /bin/echo "Project Node runtime already verified: $installed_version"
  exit 0
fi
[ ! -e "$TARGET" ] || {
  /bin/echo "Refusing to overwrite non-runtime path: $TARGET" >&2
  exit 73
}

/bin/mkdir -p "$RUNTIME_ROOT"
temporary="$(/usr/bin/mktemp -d "$RUNTIME_ROOT/.node-install.XXXXXX")"
archive_path="$temporary/$ARCHIVE"
checksums_path="$temporary/SHASUMS256.txt"
/usr/bin/curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-time 120 \
  -o "$checksums_path" "$NODE_DIST_URL/SHASUMS256.txt"
manifest_sha256="$({ /usr/bin/awk -v archive="$ARCHIVE" '$2 == archive { print $1; exit }' "$checksums_path"; } || true)"
if ! /bin/echo "$manifest_sha256" | /usr/bin/grep -Eq '^[0-9a-f]{64}$'; then
  /bin/echo "Node release checksum manifest did not contain $ARCHIVE." >&2
  exit 65
fi
if [ -n "$PINNED_SHA256" ] && [ "$manifest_sha256" != "$PINNED_SHA256" ]; then
  /bin/echo "Node release checksum no longer matches the pinned Apple Silicon checksum." >&2
  exit 65
fi

/usr/bin/curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --max-time 120 \
  -o "$archive_path" "$NODE_DIST_URL/$ARCHIVE"
actual_sha256="$(/usr/bin/shasum -a 256 "$archive_path" | /usr/bin/cut -d ' ' -f 1)"
[ "$actual_sha256" = "$manifest_sha256" ] || {
  /bin/echo "Node archive checksum mismatch; refusing to install." >&2
  exit 65
}

/usr/bin/tar -xJf "$archive_path" -C "$temporary"
/bin/mv "$temporary/node-$VERSION-$NODE_ARCH" "$TARGET"
/bin/rm -- "$archive_path"
/bin/rm -- "$checksums_path"
/bin/rmdir -- "$temporary"
temporary=""

installed_version="$("$TARGET/bin/node" --version)"
/bin/echo "Installed project Node runtime: $installed_version"
