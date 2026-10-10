#!/usr/bin/env bash
# Launch the host UI. For shot archives use scripts/kd360s_sim.py run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# --- 1. SDL2 check ---
if ! command -v sdl2-config >/dev/null 2>&1; then
  echo "!! SDL2 is not installed. The simulator needs it to open a window." >&2
  echo "   Install it, then re-run this script:" >&2
  echo "     Linux:   sudo apt install libsdl2-dev" >&2
  echo "     macOS:   brew install sdl2" >&2
  echo "     Windows: use WSL2, then: sudo apt install libsdl2-dev" >&2
  exit 1
fi

# --- 1b. Icon font (15MB, fetched not committed) ---
"$(dirname "$0")/fetch_fonts.sh"

# --- 2. venv + ESPHome ---
VENV="$ROOT/.venv"
if [ ! -d "$VENV" ]; then
  echo ">> Creating Python venv (.venv)..."
  PYTHON="${PYTHON:-python3.12}"
  if ! "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 12)' 2>/dev/null; then
    echo "Python 3.12+ is required. Set PYTHON to its executable path." >&2
    exit 1
  fi
  "$PYTHON" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Simulator requires Python 3.12+"'

if ! python3 -c "import esphome" >/dev/null 2>&1; then
  echo ">> Installing ESPHome into the venv (one-time, ~1 min)..."
  pip install --quiet --upgrade pip
  pip install --quiet -r firmware/requirements.txt
fi

# --- 3. secrets ---
if [ ! -f firmware/secrets.yaml ]; then
  echo ">> Creating firmware/secrets.yaml from example (placeholder WiFi)..."
  cp firmware/secrets.yaml.example firmware/secrets.yaml
fi

# --- 4. launch ---
echo ">> Launching simulator... a window will open on your screen."
echo "   Select a mode, then Start and Stop. Ctrl-C closes the simulator."
echo ""
if [ -z "${ESPHOME_BUILD_PATH:-}" ]; then
  SIM_BUILD="$(mktemp -d "${TMPDIR:-/tmp}/openprimus-sim.XXXXXX")"
  export ESPHOME_BUILD_PATH="$SIM_BUILD"
  trap 'rm -rf "$SIM_BUILD"' EXIT
fi
esphome run firmware/sim.yaml
