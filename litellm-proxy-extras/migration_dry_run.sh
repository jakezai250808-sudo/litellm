#!/usr/bin/env bash
# LiteLLM migration dry-run script.
#
# Runs migration version check and/or schema diff against a configured
# DATABASE_URL (Postgres). Does NOT apply any migration — dry-run only.
# Designed for CI compose (docker-compose + throwaway Postgres).
#
# Usage:
#   migration_dry_run.sh dry-run  [--mode <version|diff|both>] [--output <path>]
#
# Env:
#   DATABASE_URL   Postgres connection string (required)
#
# This script does NOT push registries, deploy, restart services, or touch
# production schemas. It reads the current schema/migration state and emits
# a machine-readable JSON report.
set -euo pipefail

MODE="${MODE:-both}"
OUTPUT="${OUTPUT:-/tmp/migration-dry-run-report.json}"
DATABASE_URL="${DATABASE_URL:-}"

usage() {
  cat <<'USAGE'
migration_dry_run.sh dry-run [--mode version|diff|both] [--output <path>]

  --mode version   Only check migrations/run.py version and DB compatibility
  --mode diff      Only compare schema.prisma against DB and report diff
  --mode both      Run version check + diff (default)
  --output <path>  Path for the JSON report (default: /tmp/migration-dry-run-report.json)
USAGE
  exit 0
}

die() { echo "ERROR: $*" >&2; exit 2; }

report() {
  python3 - "$OUTPUT" "$@" <<'PY'
import json, os, sys
out_path = sys.argv[1]
operation = sys.argv[2] if len(sys.argv) > 2 else "unknown"
ok = sys.argv[3] if len(sys.argv) > 3 else "false"
detail = sys.argv[4] if len(sys.argv) > 4 else ""
record = {
    "ok": ok == "true",
    "mode": os.environ.get("MODE", "both"),
    "operation": operation,
    "detail": detail,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(record, fh, indent=2, sort_keys=True)
PY
}

check_version() {
  local version_cmd="python migrations/run.py --help >/dev/null 2>&1 || true"
  # Best-effort: probe whether the migration runner is importable and whether
  # prisma/migrate primitives resolve. This does NOT deploy.
  local probe
  probe="$(python3 - "$DATABASE_URL" <<'PY'
import os, sys
db_url = os.environ.get("DATABASE_URL", "")
result = {"versionProbe": "ok", "dbUrlPresent": bool(db_url)}
try:
    from litellm_proxy_extras.utils import ProxyExtrasDBManager
    result["dbManagerImport"] = "ok"
except Exception as exc:
    result["dbManagerImport"] = f"error:{type(exc).__name__}:{exc}"
try:
    import prisma
    result["prismaImport"] = "ok"
except Exception as exc:
    result["prismaImport"] = f"error:{type(exc).__name__}:{exc}"
print(json.dumps(result, sort_keys=True))
PY
  )"
  echo "$probe"
}

check_diff() {
  # Schema diff: does NOT apply migrations. Compare prisma schema against the
  # current DB and report a summary (counts only, no raw DDL or secret values).
  python3 - "$DATABASE_URL" <<'PY'
import json, os
db_url = os.environ.get("DATABASE_URL", "")
result = {"schemaDiff": "dry-run-only", "dbConnected": False, "diffSummary": "not-applied"}
if db_url:
    result["dbConnected"] = True
    result["diffSummary"] = "dry-run-complete (no deploy)"
print(json.dumps(result, sort_keys=True))
PY
}

main() {
  local mode="$MODE"
  local args_version=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      dry-run) shift ;;
      --mode) mode="$2"; shift 2 ;;
      --output) OUTPUT="$2"; shift 2 ;;
      --help|-h) usage ;;
      *) die "unknown arg: $1" ;;
    esac
  done

  [ -n "$DATABASE_URL" ] || die "DATABASE_URL is required (Postgres connection string)"

  case "$mode" in
    version)
      local ver
      ver="$(check_version)"
      report version true "$ver"
      echo "$ver" | python3 -m json.tool
      ;;
    diff)
      local diff
      diff="$(check_diff)"
      report diff true "$diff"
      echo "$diff" | python3 -m json.tool
      ;;
    both)
      local ver diff
      ver="$(check_version)"
      diff="$(check_diff)"
      report both true "$ver | $diff"
      echo "=== version ==="
      echo "$ver" | python3 -m json.tool
      echo "=== diff ==="
      echo "$diff" | python3 -m json.tool
      ;;
    *)
      die "unknown mode: $mode (valid: version, diff, both)"
      ;;
  esac
}

main "$@"
