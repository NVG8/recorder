#!/usr/bin/env bash
# Build the Recorder.app bundle.
#
# Steps:
#   1. swift build -c <config> to produce the executable
#   2. Assemble Recorder.app/Contents/{MacOS,Resources}
#   3. Copy Info.plist, executable, and python/ sidecar into the bundle
#   4. Ad-hoc codesign with the entitlements file
#
# Usage: scripts/build.sh [release|debug]   (default: release)

set -euo pipefail

CONFIG="${1:-release}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Recorder"
APP_DIR="$ROOT/build/$APP_NAME.app"

echo "→ swift build -c $CONFIG"
cd "$ROOT"
swift build -c "$CONFIG"

BIN_PATH="$(swift build -c "$CONFIG" --show-bin-path)/$APP_NAME"
if [[ ! -x "$BIN_PATH" ]]; then
    echo "executable not found at $BIN_PATH" >&2
    exit 1
fi

echo "→ assembling $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

cp "$BIN_PATH" "$APP_DIR/Contents/MacOS/$APP_NAME"
cp "$ROOT/Resources/Info.plist" "$APP_DIR/Contents/Info.plist"

# Bundle python sidecar (sources only — the .venv stays at the source location,
# located at runtime via the python/ directory by uv).
mkdir -p "$APP_DIR/Contents/Resources/python"
# Every top-level .py, not a hand-kept list: a file missing from a list fails
# silently at runtime, which is how the calendar lookup once quietly broke.
cp "$ROOT/python/pyproject.toml" "$ROOT/python/uv.lock" "$APP_DIR/Contents/Resources/python/"
for f in "$ROOT"/python/*.py; do
    [[ "$(basename "$f")" == test_* ]] && continue
    cp "$f" "$APP_DIR/Contents/Resources/python/"
done

# Vendored, self-contained meeting-prep code (no external project references).
# Must be bundled or fetch_meeting_prep.py can't import meeting_prep at runtime.
if [[ -d "$ROOT/python/_prep" ]]; then
    rm -rf "$APP_DIR/Contents/Resources/python/_prep"
    cp -R "$ROOT/python/_prep" "$APP_DIR/Contents/Resources/python/_prep"
    rm -rf "$APP_DIR/Contents/Resources/python/_prep/__pycache__"
fi

echo "→ ad-hoc codesign with entitlements"
codesign --force --deep --sign - \
    --entitlements "$ROOT/Resources/Recorder.entitlements" \
    "$APP_DIR"

echo "✓ built $APP_DIR"
echo
echo "Run with:    open $APP_DIR"
echo "First launch will prompt for Screen Recording permission."
