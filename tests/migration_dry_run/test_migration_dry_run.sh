#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SCRIPT="$ROOT/litellm-proxy-extras/migration_dry_run.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_contains() {
  if ! cat /dev/stdin | grep -Fq -- "$1"; then fail "expected output to contain: $1"; fi
}

bash -n "$SCRIPT"

# 1. --help prints usage
echo "=== 1. help ==="
bash "$SCRIPT" --help 2>&1 | assert_contains 'migration_dry_run.sh'

# 2. Missing DATABASE_URL -> exit 2
echo "=== 2. no DATABASE_URL ==="
set +e; out="$(bash "$SCRIPT" dry-run 2>&1)"; rc=$?; set -e
[ $rc -eq 2 ] || fail "expected exit 2, got $rc"
echo "$out" | assert_contains 'DATABASE_URL is required'

# 3. Version check with fake DATABASE_URL — produces JSON report (import may
#    fail without litellm installed; the script probes gracefully).
echo "=== 3. version check ==="
report="$TMP/report.json"
DATABASE_URL="postgresql://fake:fake@localhost:5432/fake" MODE=version bash "$SCRIPT" dry-run --output "$report" 2>&1 | head >/dev/null || true
[ -f "$report" ] || fail "report file not created"
python3 -c "import json; d=json.load(open('$report')); assert d['ok']==True; assert d['mode']=='version'" || fail "report validation failed"
echo "report ok"

# 4. Diff check with fake DATABASE_URL
echo "=== 4. diff check ==="
report2="$TMP/report2.json"
DATABASE_URL="postgresql://fake:fake@localhost:5432/fake" MODE=diff bash "$SCRIPT" dry-run --output "$report2" 2>&1 | head >/dev/null || true
[ -f "$report2" ] || fail "report2 not created"
python3 -c "import json; d=json.load(open('$report2')); assert d['ok']==True; assert d['mode']=='diff'" || fail "diff report validation failed"

# 5. Both mode
echo "=== 5. both mode ==="
report3="$TMP/report3.json"
DATABASE_URL="postgresql://fake:fake@localhost:5432/fake" MODE=both bash "$SCRIPT" dry-run --output "$report3" 2>&1 | head >/dev/null || true
python3 -c "import json; d=json.load(open('$report3')); assert d['ok']==True; assert d['mode']=='both'" || fail "both report validation failed"

# 6. Unknown mode -> exit 2
echo "=== 6. unknown mode ==="
set +e; out="$(DATABASE_URL="postgresql://fake" MODE=bogus bash "$SCRIPT" dry-run 2>&1)"; rc=$?; set -e
[ $rc -eq 2 ] || fail "expected exit 2 for unknown mode, got $rc"

echo "PASS migration-dry-run"
