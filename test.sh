#!/usr/bin/env bash
#
# Whoogle Search - test runner
#
# Usage:
#   ./test.sh                 Run the complete unit test suite
#   ./test.sh unit            Same as above (explicit)
#   ./test.sh ratelimit       Run only the rate-limit unit tests
#   ./test.sh lint            Syntax-compile all python sources
#   ./test.sh manual          Print step-by-step manual testing instructions
#
# Rate-limit thresholds are configured with environment variables:
#   WHOOGLE_RATELIMIT_ENABLED (1/0, default 1)
#   WHOOGLE_RATELIMIT_IP_LIMIT / _IP_BURST / _IP_WINDOW
#   WHOOGLE_RATELIMIT_SESSION_LIMIT / _SESSION_BURST / _SESSION_WINDOW
#
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"

# Pick an available pytest runner
if "$PYTHON" -m pytest --version >/dev/null 2>&1; then
    PYTEST=("$PYTHON" -m pytest)
elif command -v pytest >/dev/null 2>&1; then
    PYTEST=(pytest)
else
    echo "ERROR: pytest is not installed. Install with:" >&2
    echo "  $PYTHON -m pip install -e .[test]" >&2
    exit 1
fi

# Static-file setup expected by the test suite (mirrors ./run test)
export APP_ROOT="$SCRIPT_DIR/app"
export STATIC_FOLDER="$APP_ROOT/static"

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

syntax_check() {
    echo "==> Compiling all python sources"
    "$PYTHON" -m compileall -q app test
    echo "OK"
}

case "${1:-unit}" in
    unit|"")
        syntax_check
        echo "==> Running full unit test suite"
        "${PYTEST[@]}" -sv test
        ;;
    ratelimit)
        syntax_check
        echo "==> Running rate-limit unit tests"
        "${PYTEST[@]}" -sv test/test_ratelimit.py
        ;;
    lint)
        syntax_check
        ;;
    manual)
        cat <<'MANUAL'
============================================================
Manual testing checklist for the /search + /autocomplete
dual-layer (IP + session) rate limiter
============================================================

1) Start the server with tight limits (in one terminal):

   export WHOOGLE_RATELIMIT_IP_LIMIT=5
   export WHOOGLE_RATELIMIT_IP_BURST=5
   export WHOOGLE_RATELIMIT_IP_WINDOW=60
   export WHOOGLE_RATELIMIT_SESSION_LIMIT=10
   export WHOOGLE_RATELIMIT_SESSION_BURST=10
   export WHOOGLE_RATELIMIT_SESSION_WINDOW=60
   ./run

   (Or with the module directly: python3 -m app --host 127.0.0.1 --port 5000)

2) Burst tolerance + 429 + Retry-After (IP layer):

   for i in $(seq 1 7); do
     curl -s -o /dev/null -w "request $i -> %{http_code} \
(retry-after: %header{retry-after})\n" \
       "http://127.0.0.1:5000/search?q=hello"
   done

   Expect: 200 for the first 5 requests, then 429 with a
   Retry-After header (seconds until a token refills).

3) JSON 429 body:

   curl -s -H 'Accept: application/json' \
     "http://127.0.0.1:5000/search?q=hello&format=json" | python3 -m json.tool

   Expect fields: error, error_message, rate_limit_scope, retry_after.

4) Autocomplete shares the same IP bucket:

   curl -s -o /dev/null -w "%{http_code}\n" \
     "http://127.0.0.1:5000/autocomplete?q=test"

5) Separate IPs have independent buckets (simulated X-Forwarded-For):

   curl -s -o /dev/null -w "%{http_code}\n" -H \
     'X-Forwarded-For: 203.0.113.7' \
     "http://127.0.0.1:5000/search?q=hello"   # expect 200

   Proxy chain (leftmost = original client):

   curl -s -o /dev/null -w "%{http_code}\n" -H \
     'X-Forwarded-For: 203.0.113.7, 10.0.0.1' \
     "http://127.0.0.1:5000/search?q=hello"   # still 429 for that client

6) Session layer: two different XFF IPs sharing one cookie jar are
   limited by the session bucket:

   rm -f /tmp/whoogle_cookies
   for ip in 192.0.2.1 192.0.2.2 192.0.2.3; do
     curl -s -b /tmp/whoogle_cookies -c /tmp/whoogle_cookies -o /dev/null \
       -w "$ip -> %{http_code}\n" -H "X-Forwarded-For: $ip" \
       "http://127.0.0.1:5000/search?q=hello"
   done

7) Health/metrics endpoint (stays 200 even while search is limited):

   curl -s -o /dev/null -w "healthz -> %{http_code}\n" \
     "http://127.0.0.1:5000/healthz"
   curl -s "http://127.0.0.1:5000/healthz?metrics=1" | python3 -m json.tool

8) Master switch (limiter disabled):

   WHOOGLE_RATELIMIT_ENABLED=0 ./run
   # repeat step 2 -> all requests should return 200

9) WHOOGLE_CONFIG_DISABLE must not disable rate limiting:

   WHOOGLE_CONFIG_DISABLE=1 ./run   # plus the tight limits from step 1
   # repeat step 2 -> 429s still occur; POST /config stays blocked (403)

10) Auth ordering: with WHOOGLE_USER/WHOOGLE_PASS set, unauthenticated
    requests return 401 while tokens remain, then 429 once the bucket is
    exhausted (rate limiting happens in before_request, before
    auth_required).

11) Wait one IP window (60s with the settings above) and repeat step 2:
    requests should succeed again as tokens refill.
============================================================
MANUAL
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        echo "Unknown command: $1" >&2
        usage
        exit 1
        ;;
esac
