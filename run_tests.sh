#!/usr/bin/env bash
# Run this plugin's tests the same way CI does.
# Usage: ./run_tests.sh [path-to-FiestaBoard-core]   (default: ../FiestaBoard)
#
# The core checkout must carry the output-plugin API (src/outputs/plugin_base.py):
# FiestaBoard 10.0.0 or later, or the `next` branch until 10.0.0 is tagged.
set -euo pipefail

CORE="${1:-../FiestaBoard}"
if [ ! -f "$CORE/src/outputs/plugin_base.py" ]; then
  echo "FiestaBoard core with the output-plugin API not found at: $CORE" >&2
  echo "Pass the path to your FiestaBoard checkout: ./run_tests.sh /path/to/FiestaBoard" >&2
  exit 1
fi
CORE_ABS="$(cd "$CORE" && pwd)"
PLUGIN_ID=$(python3 -c "import json; print(json.load(open('manifest.json'))['id'])")

# Recreate the import scaffold CI builds (ignored by git).
mkdir -p plugins
touch plugins/__init__.py
[ -e "plugins/$PLUGIN_ID" ] || ln -s .. "plugins/$PLUGIN_ID"

# The network fence: tests may only connect to loopback (the mock Pixoo).
PYTHONPATH="$(pwd):$CORE_ABS" python3 -m pytest tests/ -v \
  -p no:cacheprovider \
  --disable-socket --allow-hosts=127.0.0.1 \
  --cov=. --cov-config=.coveragerc --cov-report=term-missing --cov-fail-under=80
