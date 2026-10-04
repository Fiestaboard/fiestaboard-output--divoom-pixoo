#!/usr/bin/env bash
# Run this output plugin's tests the same way CI does.
# Usage: ./run_tests.sh [path-to-FiestaBoard-core] [extra pytest args...]
#        (default core path: ../FiestaBoard)
#
# The core checkout must carry the output-plugin API (src/outputs/plugin_base.py)
# (self.http, write_transition): the output-plugins stack top or `next`, or
# FiestaBoard 10.0.0 or later once it is tagged.
set -euo pipefail

CORE="${1:-../FiestaBoard}"
shift || true
if [ ! -f "$CORE/src/outputs/plugin_base.py" ]; then
  echo "FiestaBoard core with the output-plugin API not found at: $CORE" >&2
  echo "Pass the path to your FiestaBoard checkout: ./run_tests.sh /path/to/FiestaBoard" >&2
  exit 1
fi
CORE_ABS="$(cd "$CORE" && pwd)"
PLUGIN_ID=$(python3 -c "import json; print(json.load(open('manifest.json'))['id'])")

# The import scaffold (ignored by git): the tests import this plugin as
# plugins.<id>, the name FiestaBoard gives it when it loads it.
SCAFFOLD=.test-scaffold
mkdir -p "$SCAFFOLD/plugins"
touch "$SCAFFOLD/plugins/__init__.py"
[ -e "$SCAFFOLD/plugins/$PLUGIN_ID" ] || ln -s ../.. "$SCAFFOLD/plugins/$PLUGIN_ID"

# The repository root is itself a package (__init__.py) whose directory
# name (fiestaboard-output--<name>) is not an identifier: importlib mode
# with the rootdir one level up lets pytest import it anyway.
# The network fence: tests may only connect to loopback.
PYTHONPATH="$(pwd)/$SCAFFOLD:$CORE_ABS" python3 -m pytest tests/ -v \
  --rootdir=.. --import-mode=importlib \
  -p no:cacheprovider \
  --disable-socket --allow-hosts=127.0.0.1 \
  --cov=. --cov-config=.coveragerc --cov-report=term-missing --cov-fail-under=80 \
  "$@"
