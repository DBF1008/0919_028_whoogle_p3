#!/usr/bin/env bash
#
# Run the Whoogle unit test suite manually.
#
# Usage:
#   ./test.sh                 # run every test file
#   ./test.sh test/test_rate_limit.py
#   ./test.sh -k rate_limit   # extra pytest args are passed through
#
# Python selection (first match wins):
#   1. $PYTHON env var, e.g. PYTHON=/path/to/python ./test.sh
#   2. python3 on PATH with all required dependencies installed
#   3. /private/tmp/whoogle_test_env/bin/python if it exists
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON:-}"

deps_ok() {
    "$1" - >/dev/null 2>&1 <<'PY_DEPS'
import importlib
import sys
for mod in ("flask", "pytest", "brotli", "cssutils", "validators",
            "waitress", "dotenv", "stem", "httpx", "cryptography",
            "bs4", "dateutil"):
    importlib.import_module(mod)
sys.exit(0)
PY_DEPS
}

if [[ -z "$PYTHON_BIN" ]]; then
    if command -v python3 >/dev/null 2>&1 && deps_ok python3; then
        PYTHON_BIN="$(command -v python3)"
    elif [[ -x /private/tmp/whoogle_test_env/bin/python ]] \
            && deps_ok /private/tmp/whoogle_test_env/bin/python; then
        PYTHON_BIN=/private/tmp/whoogle_test_env/bin/python
    else
        echo "error: no Python interpreter with the test dependencies was found." >&2
        echo "Install requirements first: pip install -r requirements.txt" >&2
        echo "or point PYTHON at an interpreter that already has them:" >&2
        echo "  PYTHON=/path/to/python ./test.sh" >&2
        exit 1
    fi
fi

echo "Using interpreter: $PYTHON_BIN"
echo "----------------------------------------"
exec "$PYTHON_BIN" -m pytest "$@"
