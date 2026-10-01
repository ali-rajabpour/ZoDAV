#!/bin/sh
# Deployment smoke test: run compose.yaml the way a user does (./zodav setup,
# then up), push one attachment through, wait for a real backup, and run every
# ./zodav maintenance command. Needs Docker with Compose v2; run from anywhere:
#   tests/smoke.sh
# It refuses to run if a .env already exists, so it never touches a real setup.
set -eu

cd "$(dirname "$0")/.."

# A separate project name keeps the volumes and networks away from a real
# ZoDAV running on the same machine.
export COMPOSE_PROJECT_NAME=zodav-smoke
REST=smoke-rest
# restic/rest-server 0.14.0
REST_IMAGE=restic/rest-server:0.14.0@sha256:d2aff06f47eb38637dff580c3e6bce4af98f386c396a25d32eb6727ec96214a5
REPORT="zodav-report-$(date +%Y-%m-%d).html"

fail() { printf 'smoke: FAIL: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

[ ! -e .env ] || fail ".env already exists; move it away first"
[ ! -e "$REPORT" ] || fail "$REPORT already exists; move it away first"
docker info >/dev/null 2>&1 || fail "Docker is not running"

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    docker compose --profile backup logs --no-color --tail 40 || true
  fi
  docker compose --profile backup down -v --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "$REST" >/dev/null 2>&1 || true
  rm -f .env "$REPORT"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# until <seconds> <description> <command...>: poll once a second.
until_ok() {
  limit=$1 what=$2
  shift 2
  n=0
  while ! "$@"; do
    n=$((n + 1))
    [ "$n" -lt "$limit" ] || fail "timed out waiting for $what"
    sleep 1
  done
}

backup_ok() { docker compose logs --no-color backup 2>/dev/null | grep -q 'backup ok'; }
report_saved() { ./zodav report >/dev/null 2>&1 && [ -s "$REPORT" ]; }

step "setup"
printf 'tskey-auth-smoke-not-a-real-key\n\n\nrest:http://%s:8000/\n' "$REST" | ./zodav setup
grep -q '^COMPOSE_PROFILES=backup$' .env || fail "setup did not enable the backup profile"

step "start webdav and audit"
docker compose up -d --build --wait webdav audit

step "start the restic rest server"
docker rm -f "$REST" >/dev/null 2>&1 || true
docker run -d --name "$REST" --network "${COMPOSE_PROJECT_NAME}_internal" "$REST_IMAGE" rest-server --no-auth >/dev/null

step "write one attachment through the audit container"
docker compose run --rm -T --entrypoint python audit - <<'PY'
import hashlib
import io
import os
import sys
import zipfile

sys.path.insert(0, "/app")
from zodav_audit import DavStore

body = b"smoke attachment"
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
    z.writestr(zipfile.ZipInfo("smoke.txt", (2024, 1, 1, 0, 0, 0)), body)
prop = (
    '<properties version="1"><mtime>1700000000000</mtime>'
    f'<hash>{hashlib.md5(body).hexdigest()}</hash></properties>'
).encode()
store = DavStore("http://webdav:8080/", "zotero", os.environ["ZODAV_AUDIT_PASSWORD"])
store.write("SMKTEST2.zip", buf.getvalue())
store.write("SMKTEST2.prop", prop)
PY

step "start the backup and wait for it"
docker compose up -d backup
until_ok 120 "a successful backup" backup_ok

for cmd in check restore-drill "repair" accept-deletions; do
  step "./zodav $cmd"
  ./zodav "$cmd" || fail "./zodav $cmd exited $?"
done

step "./zodav report"
until_ok 60 "the first audit report" report_saved

printf '\nsmoke: OK\n'
