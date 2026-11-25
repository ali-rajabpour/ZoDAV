FROM restic/restic:0.20.0@sha256:93999df80d4ac6cb148e82d2ef1fe6a81996f17ff535e772723538211b31555a

# Runs as nobody on a read-only root. The directories are created here so a
# fresh named volume mounted on them inherits the ownership.
# apk upgrade pulls Alpine security fixes (e.g. openssh-client) that land
# before the next restic release.
RUN apk upgrade --no-cache \
 && mkdir -p /app /repo /state /cache \
 && chown 65534:65534 /repo /state /cache
COPY --chmod=0755 run.sh /app/run.sh

ENV RESTIC_CACHE_DIR=/cache
USER 65534:65534
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD ["/bin/sh", "-c", "t=$(cat /state/last-success 2>/dev/null) && [ $(( $(date +%s) - t )) -lt ${BACKUP_MAX_AGE_SECONDS:-93600} ]"]
ENTRYPOINT ["/bin/sh", "/app/run.sh"]
