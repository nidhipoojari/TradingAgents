#!/usr/bin/env bash
# Optional, testing only: Playwright + headless Chromium for scripts/smoke/live_dashboard.py.
# The app never needs this. Usage:
#   scripts/smoke/setup_playwright.sh [--python PATH] [--user-libs]
# --python     the Python to install Playwright into (default: .venv/bin/python)
# --user-libs  if Chromium's system libraries are missing and you cannot use sudo, download them into
#              .playwright-libs/ (apt-get download; no root) and print the LD_LIBRARY_PATH to use.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
USER_LIBS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PY="$2"; shift 2 ;;
    --user-libs) USER_LIBS=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# 1. The pinned Playwright from the e2e extra in pyproject.toml (only that package).
PIN=$("$PY" -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["optional-dependencies"]["e2e"][0])')
"$PY" -m pip install --quiet "$PIN"
echo "installed $PIN into $PY"

# 2. Headless Chromium. Playwright 1.52 has no build for Ubuntu 26.04; its Ubuntu 24.04 build runs there.
if grep -qs 'VERSION_ID="26\.' /etc/os-release && [ -z "${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-}" ]; then
  export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64
  echo "Ubuntu 26.04 detected: using PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 (set it when running too)"
fi
# The library check below replaces Playwright's own (which fails with a stack trace when they are missing).
PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1 "$PY" -m playwright install chromium-headless-shell

# 3. Chromium's system libraries.
MISSING=()
for lib in libnss3.so libnspr4.so libasound.so.2; do
  ldconfig -p 2>/dev/null | grep -q "$lib" || MISSING+=("$lib")
done
if [ ${#MISSING[@]} -eq 0 ]; then
  echo "system libraries present; ready"
  exit 0
fi
echo "missing system libraries: ${MISSING[*]}"
if [ "$USER_LIBS" -eq 1 ]; then
  mkdir -p .playwright-libs/debs
  (cd .playwright-libs/debs && apt-get download libnss3 libnspr4 libasound2t64)
  for deb in .playwright-libs/debs/*.deb; do dpkg -x "$deb" .playwright-libs/root; done
  echo "unpacked without root; run the test with:"
  echo "  export LD_LIBRARY_PATH=$PWD/.playwright-libs/root/usr/lib/x86_64-linux-gnu"
else
  echo "install them with: sudo apt-get install libnss3 libnspr4 libasound2t64"
  echo "or, without root:  scripts/smoke/setup_playwright.sh --user-libs"
fi
