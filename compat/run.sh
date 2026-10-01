#!/bin/sh
# Start several WebDAV servers, run `zodav-audit conformance` against each and
# write compat/RESULTS.md. Servers failing the audit is data, not an error:
# exit is non-zero only when the harness itself breaks.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
COMPOSE="docker compose -p zodav-compat -f $ROOT/compat/compose.yaml"
OUT=$ROOT/compat/RESULTS.md
USER_NAME=compat
ZODAV_AUDIT_PASSWORD=compat-password-0123456789abcdef
export ZODAV_AUDIT_PASSWORD
TMP=$(mktemp -d)

cleanup() {
    # shellcheck disable=SC2086
    $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# name|port|base path|service
SERVERS='ZoDAV|18180||zodav
rclone serve webdav|18181||rclone
hacdias/webdav|18182||hacdias
nginx (ngx_http_dav_module)|18183||nginx
Nextcloud|18184|/remote.php/dav/files/compat|nextcloud'

version_of() {
    if [ "$1" = zodav ]; then
        echo "local build"
        return
    fi
    sed -n "/^  $1:/,/^$/s/^ *image: *[^:]*:\\([^@]*\\)@.*/\\1/p" "$ROOT/compat/compose.yaml" | head -n 1
}

http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true
}

# Zotero's "create folder" dialog is one MKCOL; 405 means it already exists.
wait_ready() {
    port=$2
    base=$3
    i=0
    while [ "$i" -lt 120 ]; do
        if [ "$1" = nextcloud ]; then
            curl -s --max-time 5 "http://127.0.0.1:$port/status.php" 2>/dev/null | grep -q '"installed":true' || { i=$((i + 1)); sleep 2; continue; }
        fi
        code=$(http_code -u "$USER_NAME:$ZODAV_AUDIT_PASSWORD" -X MKCOL "http://127.0.0.1:$port$base/zotero/")
        case $code in
            201 | 405) return 0 ;;
        esac
        i=$((i + 1))
        sleep 2
    done
    return 1
}

# shellcheck disable=SC2086
$COMPOSE up -d --build

{
    echo "Generated $(date -u +%Y-%m-%d) by compat/run.sh"
    echo
    echo "| Server | Version | Result | Failing checks |"
    echo "|---|---|---|---|"
} >"$OUT"

echo "$SERVERS" | while IFS='|' read -r name port base svc; do
    version=$(version_of "$svc")
    if ! wait_ready "$svc" "$port" "$base"; then
        echo "| $name | $version | did not start | - |" >>"$OUT"
        continue
    fi
    rc=0
    python3 "$ROOT/audit/zodav_audit.py" conformance "http://127.0.0.1:$port$base/" \
        --user "$USER_NAME" --json >"$TMP/$svc.json" || rc=$?
    if [ "$rc" -ge 2 ]; then
        echo "| $name | $version | could not run | - |" >>"$OUT"
        continue
    fi
    # Failing checks: every non-info finding code, de-duplicated.
    # shellcheck disable=SC2016
    codes=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
seen = []
for f in d["findings"]:
    if f["severity"] != "info" and f["code"] not in seen:
        seen.append(f["code"])
print(", ".join("`%s`" % c for c in seen) or "-")
' "$TMP/$svc.json")
    if [ "$rc" -eq 0 ]; then result=pass; else result=fail; fi
    echo "| $name | $version | $result | $codes |" >>"$OUT"
    cp "$TMP/$svc.json" "${COMPAT_JSON_DIR:-$TMP}/$svc.json" 2>/dev/null || true
done

cat "$OUT"
