#!/usr/bin/env bash
# One-command FreeCAD GUI smoketest runner.
# Brings up an X display if needed, syncs uv-managed deps, then runs the harness.
set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKETEST_ROOT="${SMOKETEST_ROOT:-$HOME/cua_gui_smoketest}"
ASSETS_DIR="$SMOKETEST_ROOT/assets"
SHOTS_DIR="$SMOKETEST_ROOT/screenshots"
LOGS_DIR="$SMOKETEST_ROOT/logs"
SCRIPTS_DIR="$SMOKETEST_ROOT/scripts"

mkdir -p "$ASSETS_DIR" "$SHOTS_DIR" "$LOGS_DIR" "$SCRIPTS_DIR"

# Mirror canonical scripts into the runtime tree so $SMOKETEST_ROOT is self-contained.
cp -f "$REPO_ROOT/scripts/generate_freecad_assets.py" "$SCRIPTS_DIR/"
cp -f "$REPO_ROOT/scripts/freecad_gui_smoketest.py"   "$SCRIPTS_DIR/"

# Ensure uv is on PATH (the standalone installer drops it in ~/.local/bin).
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is not installed and not on PATH." >&2
  exit 2
fi

echo "[runner] syncing python env via uv"
( cd "$REPO_ROOT" && uv sync --quiet ) || {
  echo "ERROR: uv sync failed" >&2
  exit 3
}

# Bring up a display if none usable.
if [ -z "${DISPLAY:-}" ] || ! xdpyinfo >/dev/null 2>&1; then
  echo "[runner] no usable DISPLAY; starting Xvfb :99"
  export DISPLAY=":99"
  if ! xdpyinfo >/dev/null 2>&1; then
    Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp \
      >"$LOGS_DIR/xvfb.log" 2>&1 &
    XVFB_PID=$!
    echo "$XVFB_PID" >"$LOGS_DIR/xvfb.pid"
    # Wait for display.
    for _ in $(seq 1 40); do
      sleep 0.25
      xdpyinfo >/dev/null 2>&1 && break
    done
    if command -v openbox >/dev/null 2>&1; then
      openbox >"$LOGS_DIR/openbox.log" 2>&1 &
      echo "$!" >"$LOGS_DIR/openbox.pid"
      sleep 1
    fi
  fi
else
  echo "[runner] reusing existing DISPLAY=$DISPLAY"
fi

xdpyinfo >/dev/null 2>&1 || {
  echo "ERROR: no usable X display after attempting to start Xvfb." >&2
  exit 4
}

echo "[runner] running smoketest"
set +e
( cd "$REPO_ROOT" && uv run python "$REPO_ROOT/scripts/freecad_gui_smoketest.py" )
RC=$?
set -e

echo
echo "===== ARTIFACT LISTING ====="
echo "Assets:"
ls -1 "$ASSETS_DIR" 2>/dev/null | sed 's/^/  /'
echo "Screenshots:"
ls -1 "$SHOTS_DIR" 2>/dev/null | sed 's/^/  /'
echo "Logs:"
ls -1 "$LOGS_DIR" 2>/dev/null | sed 's/^/  /'

# Only fail if we produced no screenshots.
SHOT_COUNT=$(find "$SHOTS_DIR" -maxdepth 1 -name '*.png' | wc -l)
if [ "$SHOT_COUNT" -eq 0 ]; then
  echo "ERROR: no screenshots produced" >&2
  exit 5
fi

exit $RC
