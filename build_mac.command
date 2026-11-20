#!/bin/bash
# ---------------------------------------------------------------------------
#  Builds "dist/Changi Departures.app" for macOS.
#  Double-click this file in Finder (or run:  bash build_mac.command).
#  Needs Python 3.10 or newer from python.org (the python3 that comes with macOS can't
#  build apps) and Apple's Command Line Tools (the script offers to install them).
#  Everything this script prints is also saved to build_log.txt next to it.
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1
LOG="$(pwd)/build_log.txt"
exec > >(tee "$LOG") 2>&1

pause() { echo; read -r -p "Press Enter to close this window..." _; }
fail() {
  echo
  echo "================================================================"
  echo " BUILD FAILED: $1"
  echo " The full log is saved in: $LOG"
  echo "================================================================"
  pause; exit 1
}

echo "Changi Departures - Mac build   $(date)"
echo "macOS $(sw_vers -productVersion 2>/dev/null)  ($(uname -m))"
echo

# 0. Apple's Command Line Tools (lipo, codesign, otool) are needed to build a Mac app
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Apple's Command Line Tools are not installed. Opening the installer..."
  xcode-select --install 2>/dev/null
  fail "Apple's Command Line Tools are needed to build the app.
 A window should have opened: click Install (about 5-15 minutes).
 When it has finished, double-click this file again."
fi
echo "Apple Command Line Tools: $(xcode-select -p)"

# 1. Find a Python that can build apps: 3.10+ and NOT Apple's built-in /usr/bin/python3
PY=""
# python.org installs (newest version first), then Homebrew, then whatever "python3" is.
CANDIDATES=$(ls -d /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 2>/dev/null | sort -t. -k3,3nr)
for c in $CANDIDATES /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  [ "$p" = "/usr/bin/python3" ] && { echo "Skipping $p (Apple's built-in Python can't build apps)"; continue; }
  if "$p" -c 'import sys, sysconfig; ok = sys.version_info >= (3, 10) and bool(sysconfig.get_config_var("PYTHONFRAMEWORK") or sysconfig.get_config_var("Py_ENABLE_SHARED")); sys.exit(0 if ok else 1)' 2>/dev/null; then
    PY="$p"; break
  else
    echo "Skipping $p ($("$p" --version 2>&1)): too old, or built without the shared library PyInstaller needs"
  fi
done
if [ -z "$PY" ]; then
  fail "No suitable Python found.
 Install Python 3.12 or newer from https://www.python.org/downloads/macos/
 (choose the 'macOS 64-bit universal2 installer'), then double-click this file again."
fi
echo "Using Python: $PY  ($("$PY" --version 2>&1))"
echo

# 2. Fresh build environment every time (an old one may have been made with the wrong Python)
rm -rf .venv-mac build dist
"$PY" -m venv .venv-mac || fail "Could not create a Python environment."
VPY="$(pwd)/.venv-mac/bin/python"
echo "Installing libraries (needs internet, about 1-2 minutes)..."
"$VPY" -m pip install --upgrade pip >/dev/null || fail "Could not update pip. Check the internet connection."
"$VPY" -m pip install -r requirements.txt pyinstaller || fail "Could not install the libraries (see the messages above). Check the internet connection."
echo

# 3. Build
echo "Building the app (about 2-4 minutes)..."
"$VPY" -m PyInstaller --noconfirm --clean changi_departures.spec || fail "PyInstaller stopped with an error - see the last lines above."
APP="dist/Changi Departures.app"
if [ ! -d "$APP" ]; then
  if [ -d dist/ChangiDepartures ]; then
    fail "The program was built, but wrapping it as a .app failed (see the last lines above)."
  fi
  fail "Nothing was built (see the last lines above)."
fi

# 4. Self-test: start the built app and check that it answers
echo
echo "Testing the app..."
TEST_DIR="$(pwd)/build/selftest"
"$APP/Contents/MacOS/ChangiDepartures" --no-browser --no-menu-bar --port 8790 --output-dir "$TEST_DIR" &
PID=$!
ok=""
for _ in $(seq 1 60); do
  if curl -s -m 1 http://127.0.0.1:8790/api/config 2>/dev/null | grep -q "Changi Departures"; then ok=1; break; fi
  sleep 0.5
done
kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null
[ -z "$ok" ] && fail "The app was built but did not start. See $TEST_DIR/logs for details."
echo "Self-test passed: the app starts and its page answers."

# 5. Package for sharing (ditto keeps the app bundle intact; Finder 'Compress' also works)
ditto -c -k --keepParent "$APP" "dist/Changi Departures (Mac).zip" || fail "Could not create the zip."
echo
echo "================================================================"
echo " Done!"
echo " App:   $(pwd)/$APP"
echo " Share: $(pwd)/dist/Changi Departures (Mac).zip"
echo " Drag the app into Applications. It shows as a plane in the menu bar."
echo " (changi_departures.spec is only the build recipe.)"
echo "================================================================"
open dist
pause
