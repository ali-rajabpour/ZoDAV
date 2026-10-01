#!/bin/sh
# Backup loop: restic backup + forget/prune every interval, weekly partial
# data check. Misconfiguration is logged every interval and the container
# stays alive (unhealthy) instead of crash-looping.
INTERVAL=${BACKUP_INTERVAL_SECONDS:-86400}
CHECK_INTERVAL=${BACKUP_CHECK_INTERVAL_SECONDS:-604800}
STATE=${STATE_DIR:-/state}
SOURCE=${BACKUP_SOURCE:-/data/zotero}

# Sleep in the background and wait on it, so TERM is handled at once instead
# of after the sleep finishes.
trap 'exit 0' TERM INT

while :; do
  if [ -z "${RESTIC_REPOSITORY:-}" ]; then
    echo "BACKUP FAILED: RESTIC_REPOSITORY is not set"
  elif [ -z "${RESTIC_PASSWORD:-}" ]; then
    echo "BACKUP FAILED: RESTIC_PASSWORD is not set"
  elif [ "${#RESTIC_PASSWORD}" -lt 24 ]; then
    echo "BACKUP FAILED: RESTIC_PASSWORD is shorter than 24 characters"
  elif case $RESTIC_PASSWORD in CHANGE_ME*) true ;; *) false ;; esac; then
    echo "BACKUP FAILED: RESTIC_PASSWORD is still the CHANGE_ME placeholder"
  elif [ -z "$(find "$SOURCE" -maxdepth 1 -name '*.zip' 2>/dev/null | head -n 1)" ]; then
    # last-success is deliberately left alone, so a store that stays empty
    # eventually shows up as a stale backup.
    echo "BACKUP SKIPPED: the store is empty; not replacing good snapshots with an empty one"
  elif ! { restic cat config >/dev/null 2>&1 || restic init; }; then
    echo "BACKUP FAILED: cannot open or initialise the repository"
  elif ! restic backup --host zodav --tag zodav "$SOURCE"; then
    echo "BACKUP FAILED: restic backup returned an error"
  elif ! restic forget --prune --host zodav --tag zodav \
      --keep-daily 7 --keep-weekly 4 --keep-monthly 12; then
    echo "BACKUP FAILED: forget/prune returned an error"
  else
    date +%s > "$STATE/last-success"
    echo "backup ok"
    now=$(date +%s)
    last=$(cat "$STATE/last-check" 2>/dev/null || echo 0)
    if [ $((now - last)) -ge "$CHECK_INTERVAL" ]; then
      if restic check --read-data-subset=5%; then
        date +%s > "$STATE/last-check"
      else
        echo "BACKUP CHECK FAILED"
      fi
    fi
  fi
  sleep "$INTERVAL" &
  wait $!
done
