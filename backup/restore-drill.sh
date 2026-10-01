#!/bin/sh
# Restore the latest snapshot into a throwaway volume and run the integrity
# check on it. Usage: restore-drill.sh <compose-file>
set -u
[ $# -eq 1 ] || { echo "usage: $0 <compose-file>" >&2; exit 2; }
COMPOSE=$1
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=python:3.12-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111
VOL=zodav-restore-drill-$$

cleanup() { docker volume rm -f "$VOL" >/dev/null 2>&1; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Exit 1: integrity errors (report printed). Exit 3: clean but no attachments.
CHECK='import json, subprocess, sys
p = subprocess.run([sys.executable, "/app/zodav_audit.py", "integrity", "/restore/data/zotero", "--json"],
                   capture_output=True, text=True)
if p.returncode != 0:
    sys.stdout.write(p.stdout + p.stderr)
    sys.exit(1)
sys.exit(0 if json.loads(p.stdout)["stats"]["attachments"] > 0 else 3)'

docker volume create "$VOL" >/dev/null || { echo "restore drill FAILED: cannot create volume"; exit 1; }
# The backup image runs as nobody; a fresh volume is root-owned.
docker run --rm -v "$VOL:/restore" --entrypoint chown "$PYTHON" 65534:65534 /restore >/dev/null \
  || { echo "restore drill FAILED: cannot prepare volume"; exit 1; }

if ! docker compose -f "$COMPOSE" run --rm --no-deps -T -v "$VOL:/restore" --entrypoint restic \
    backup restore latest --host zodav --target /restore; then
  echo "restore drill FAILED: restic could not restore the latest snapshot"
  exit 1
fi

docker run --rm -v "$VOL:/restore:ro" -v "$ROOT/audit/zodav_audit.py:/app/zodav_audit.py:ro" \
    "$PYTHON" python -c "$CHECK"
case $? in
  0) ;;
  3) echo "restore drill: the latest snapshot contains no attachments"; exit 1 ;;
  *) echo "restore drill FAILED: the restored store did not pass the integrity check"; exit 1 ;;
esac
echo "restore drill ok"
