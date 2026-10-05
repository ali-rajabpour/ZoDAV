# Debian variant on purpose: mod_dav_fs keeps its lock DB in a DBM file, and
# the Alpine build of apr-util can only use gdbm, which takes a non-blocking
# exclusive flock. Concurrent PUTs then fail with 500 "Could not open the lock
# database". Debian's apr-util defaults to Berkeley DB, which does not.
FROM httpd:2.4.69@sha256:41163d514c02ac205161c1549b499bc11adfdeb6291735b433d706af7e64382a

# /data/zotero is created here, owned by www-data, so a fresh named volume
# inherits the ownership on first mount.
RUN mkdir -p /data/zotero /srv/public /run/zodav \
 && printf 'ok\n' > /srv/public/healthz \
 && chown -R www-data:www-data /data /run/zodav \
 && chmod 0755 /srv/public /srv/public/healthz

COPY httpd.conf /usr/local/apache2/conf/httpd.conf
COPY --chmod=0755 entrypoint.sh /usr/local/bin/zodav-entrypoint

# hadolint ignore=DL3066 # www-data is defined in the httpd base image
USER www-data
EXPOSE 8080
# The image has neither wget nor curl.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /healthz HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q '^HTTP/1.. 200' <&3"]
ENTRYPOINT ["/usr/local/bin/zodav-entrypoint"]
