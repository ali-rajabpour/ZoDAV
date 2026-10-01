Generated 2026-10-01 by compat/run.sh

| Server | Version | Result | Failing checks |
|---|---|---|---|
| ZoDAV | local build | pass | - |
| rclone serve webdav | 1.75.1 | pass | - |
| hacdias/webdav | v5.16.1 | pass | - |
| nginx (ngx_http_dav_module) | 1.31.6-alpine | fail | `NOT_DAV` |
| Nextcloud | 35.0.1-apache | pass | - |
