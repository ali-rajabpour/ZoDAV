#!/bin/sh
# ZoDAV: validate settings, hash the passwords into tmpfs, start Apache.
set -eu
export LC_ALL=C

RUN_DIR=/run/zodav
# htpasswd writes a temp file; the root filesystem is read-only.
export TMPDIR=$RUN_DIR
# Apache re-checks the bcrypt hash on every request (mod_authn_socache only
# caches the hash lookup, not the verification), so the cost is a per-request
# price: 10 is about 60 ms.
BCRYPT_COST=10
HTPASSWD=$RUN_DIR/htpasswd

die() {
    echo "zodav: $*" >&2
    exit 1
}

check_username() {
    case $2 in
        [a-z]*) ;;
        *) die "$1 must start with a lowercase letter" ;;
    esac
    case $2 in
        *[!a-z0-9_-]*) die "$1 may only contain a-z, 0-9, '_' and '-'" ;;
    esac
    [ "${#2}" -le 32 ] || die "$1 must be at most 32 characters"
}

check_password() {
    # .env.example placeholders are long enough to pass the length check
    case $2 in
        CHANGE_ME*) die "$1 still has the placeholder value from .env.example; run ./zodav setup" ;;
    esac
    [ "${#2}" -ge 24 ] || die "$1 must be at least 24 characters"
    case $2 in
        *[!A-Za-z0-9_-]*) die "$1 may only contain A-Z, a-z, 0-9, '_' and '-'" ;;
    esac
}


USER_MAIN=${ZODAV_USERNAME:-zotero}
PASS_MAIN=${ZODAV_PASSWORD:-}
USER_SVC=${ZODAV_SERVICE_USERNAME:-}
PASS_SVC=${ZODAV_SERVICE_PASSWORD:-}

check_username ZODAV_USERNAME "$USER_MAIN"
[ -n "$PASS_MAIN" ] || die "ZODAV_PASSWORD is required"
check_password ZODAV_PASSWORD "$PASS_MAIN"

if [ -n "$USER_SVC" ] || [ -n "$PASS_SVC" ]; then
    [ -n "$USER_SVC" ] || die "ZODAV_SERVICE_USERNAME is required when ZODAV_SERVICE_PASSWORD is set"
    [ -n "$PASS_SVC" ] || die "ZODAV_SERVICE_PASSWORD is required when ZODAV_SERVICE_USERNAME is set"
    check_username ZODAV_SERVICE_USERNAME "$USER_SVC"
    check_password ZODAV_SERVICE_PASSWORD "$PASS_SVC"
    [ "$USER_SVC" != "$USER_MAIN" ] || die "ZODAV_SERVICE_USERNAME must differ from ZODAV_USERNAME"
fi

[ -w "$RUN_DIR" ] || die "$RUN_DIR is not writable (mount a tmpfs there)"
rm -f "$HTPASSWD" "$RUN_DIR"/httpd.pid "$RUN_DIR"/DavLock*
umask 077
printf '%s' "$PASS_MAIN" | htpasswd -i -B -C "$BCRYPT_COST" -c "$HTPASSWD" "$USER_MAIN" >/dev/null 2>&1 \
    || die "could not hash the password for ZODAV_PASSWORD"
if [ -n "$USER_SVC" ]; then
    printf '%s' "$PASS_SVC" | htpasswd -i -B -C "$BCRYPT_COST" "$HTPASSWD" "$USER_SVC" >/dev/null 2>&1 \
        || die "could not hash the password for ZODAV_SERVICE_PASSWORD"
fi
unset PASS_MAIN PASS_SVC ZODAV_PASSWORD ZODAV_SERVICE_PASSWORD
umask 022

# An empty volume or tmpfs mounted over /data hides the directory created in
# the image.
if [ -w /data ]; then
    mkdir -p /data/zotero
fi

exec httpd -DFOREGROUND -f /usr/local/apache2/conf/httpd.conf
