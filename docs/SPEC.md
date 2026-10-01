# ZoDAV - design spec

ZoDAV is short for **Zo**tero Web**DAV**. Author: Ali Rajabpour Sanati
(<ali@rajabpour.com>, <https://rajabpour.com>). Repository: <https://github.com/ali-rajabpour/ZoDAV>. License: AGPL-3.0-only.

## Purpose

Two deliverables in one repository:

1. **The ZoDAV server.** A private, self-hosted WebDAV store for Zotero
   attachment files. One `compose.yaml` and one `.env` file; reachable only
   over a Tailscale or Headscale network, never from the public internet. It
   replaces paid Zotero Storage for a personal library.
2. **`zodav-audit`.** A standalone, stdlib-only Python CLI that checks *any*
   Zotero WebDAV server (ZoDAV, Nextcloud, Synology, Koofr, pCloud, nginx,
   Caddy, rclone, …):
   - `conformance` - does the server behave the way Zotero's client needs,
     and if not, which Zotero error will the user see and what causes it;
   - `integrity` - is every `.zip`/`.prop` pair in the store consistent and
     readable;
   - `repair` - fix what can be fixed safely (dry run unless `--apply`).

   The ZoDAV server runs the same tool against its own data, so the server
   gets the checks for free and the tool is exercised in production.

Why the split: private Zotero WebDAV servers already exist publicly (for
example `rlan/zotero-webdav-server`, `louisaslett/zotero-webdav`,
`gzurowski/zotero-dav`). A Zotero-aware conformance, integrity and repair
tool does not; Zotero forum users ask for one and are told to "Reset File
Sync History".

### Success means

1. A non-technical user can deploy the server with `git clone`, `./zodav
   setup`, `./zodav start`, and paste the printed values into Zotero.
2. Zotero Desktop → Settings → Sync → File Syncing → WebDAV → **Verify
   Server** succeeds, and files sync both ways.
3. Nothing is reachable from the public internet.
4. Data survives container restarts, redeploys and (with backups enabled) the
   loss of the server.
5. Corruption, mass deletion, a full disk, an idle store and failed backups
   are detected and pushed to the owner, not discovered months later.
6. `zodav-audit`, pointed at any WebDAV URL, gives results a non-expert can
   act on, and changes nothing on that server unless `repair --apply` is
   used.

Non-goals: public internet exposure, group libraries (Zotero group files
cannot use WebDAV), a web UI, multi-tenant hosting. Phone access (needs
HTTPS with a CA-signed certificate) is on the README to-do list.

## What Zotero actually does

Source: `zotero/zotero`, `chrome/content/zotero/xpcom/storage/webdav.js`.

Zotero appends `zotero/` to the configured URL. "Verify Server"
(`checkServer`):

1. `OPTIONS <url>/zotero/` - the response must carry a `DAV` header
   (otherwise `NOT_DAV`). Success codes 200, 204, 404.
2. `PROPFIND <url>/zotero/`, `Depth: 0` - 207 (exists) or 404. On 404,
   `PROPFIND` on the parent: 207 → `ZOTERO_DIR_NOT_FOUND` (Zotero offers to
   create it with `MKCOL`), 404 → `PARENT_DIR_NOT_FOUND`.
3. `GET <url>/zotero/nonexistent.prop` - must be 404; any 2xx →
   `NONEXISTENT_FILE_NOT_MISSING`.
4. `PUT <url>/zotero/zotero-test-file.prop`, body `" "` - 200/201/204.
5. `GET` that file - 200; 404 → `FILE_MISSING_AFTER_UPLOAD`.
6. `DELETE` that file - 200/204.

401 → invalid login, 403 → permission denied, 5xx → generic server error.

Normal sync uses `GET`, `PUT` (`Content-Type: application/zip`, no
timeout), `DELETE`, `PROPFIND` (`Depth: 0`, and `Depth: 1` with
`getlastmodified` for the orphan purge) and `MKCOL`. No `LOCK`, `UNLOCK`,
`PROPPATCH`, `MOVE`, `COPY`.

Storage layout for an attachment item with key `K` (8 characters from
`23456789ABCDEFGHIJKLMNPQRSTUVWXYZ`):

- `zotero/K.zip` - ZIP containing the attachment file(s);
- `zotero/K.prop` -
  `<properties version="1"><mtime>MS</mtime><hash>MD5</hash></properties>`,
  `MS` milliseconds since the epoch, `MD5` the hex MD5 of the file.

Client behaviour that turns server mistakes into data loss:

- A `.prop` whose `mtime` is not 1-10 digits (seconds) or 1-13 digits
  (milliseconds) is **deleted** by the client.
- A `404` on `GET K.zip` makes the client **delete** `K.prop`. A `200` with
  an HTML error page is saved as the file.
- The orphan purge deletes `.zip`/`.prop` files not in the sync queue whose
  `getlastmodified` is more than 7 days before the library's last sync.
  `lastsync`/`lastsync.txt` (written by old clients) are skipped; current
  clients do not update them.
- `507` on `PUT` is reported to the user as insufficient space; any other
  5xx as a generic error.

## Architecture

```
Zotero Desktop ──tailnet tcp/80──▶ [tailscale] ──bridge──▶ [webdav :8080] ── volume zodav-data
other containers on the host ──docker network "zodav"──▶ [webdav :8080]
                                [audit]  ── zodav-audit loop (read-only data mount) ──▶ alerts (webhook/Telegram/email)
                                [backup] ── restic → off-host repository (read-only data mount, optional)
```

One `compose.yaml`, no published host ports, no reverse-proxy labels.

- **webdav** - Apache httpd 2.4 (Debian image: Alpine's apr-util only has a
  gdbm lock database, which makes concurrent `PUT`s fail with 500) with
  `mod_dav` + `mod_dav_fs`,
  minimal `httpd.conf`. `/zotero/` is the only DAV location; `/healthz` is the
  only anonymous path; everything else is denied. Runs as `www-data`
  (non-root) with a read-only root filesystem; the only writable paths are
  the data volume and one tmpfs (`/run/zodav`: htpasswd, lock DB, pid).
  Apache verifies the bcrypt hash on every request (`mod_authn_socache` only
  caches the lookup), so the cost is a per-request price: cost 10 (~60 ms).
  Generated passwords are 32 random characters, so the cost factor adds no
  meaningful protection beyond that.
- **tailscale** - userspace sidecar (no `NET_ADMIN`, no `/dev/net/tun`)
  forwarding tailnet tcp/80 to `webdav:8080`. Tailscale by default;
  Headscale by setting `ZODAV_HEADSCALE_URL`.
- **audit** - `python:3.12-alpine` running `zodav-audit watch` against the
  data volume mounted read-only (see "Watch mode"). Writes JSON and HTML
  reports to a `reports` volume, prints one summary line per run, and posts
  alerts to the configured webhook, Telegram and email channels.
- **backup** (enabled by `COMPOSE_PROFILES=backup`) - `restic/restic` with a
  shell loop: `restic backup` (read-only mount), `forget --prune`
  (keep-daily 7, keep-weekly 4, keep-monthly 12), weekly
  `restic check --read-data-subset=5%`. Writes `/state/last-success`
  (shared read-only with audit) and logs `BACKUP FAILED` on failure; the
  healthcheck turns unhealthy when the last success is older than 26 hours.

Users (HTTP Basic, bcrypt cost 10, hashed at container start into tmpfs):

- `ZODAV_USERNAME` (default `zotero`) / `ZODAV_PASSWORD` - required.
- `ZODAV_SERVICE_USERNAME` / `ZODAV_SERVICE_PASSWORD` - optional second
  user for an automation that writes into the store (for example a service
  that deposits PDFs). Separate users so either can be rotated alone and the
  access log shows who wrote what.

Other containers on the same host reach the server by joining the Docker
network named `zodav` as `external` and using `http://webdav:8080/`.

## `zodav-audit`

One Python file, `audit/zodav_audit.py`, stdlib only, Python 3.10+. Runs as
`python3 zodav_audit.py …`, through `uvx --from git+<repo> zodav-audit …`,
or inside the `audit` container.

Credentials: `--user` (default `zotero`) plus the `ZODAV_AUDIT_PASSWORD`
environment variable or an interactive prompt. Never a command-line argument.

Target: a URL - exactly what the user types into Zotero; the tool appends
`zotero/` as Zotero does (plain `host` gets `https://`) - or a local
directory, which is the `zotero/` folder itself.

### `conformance <url> [--read-only]`

1. The Verify Server sequence above, in order, with Zotero's own success
   rules. A failure reports the Zotero error code the user would see, the
   request and response status line, the likely cause, and a fix.
2. Sync-path checks Verify does not cover:
   - unauthenticated `PROPFIND` → `401` with `WWW-Authenticate: Basic`;
   - `PROPFIND Depth: 1` on `zotero/` returns `getlastmodified` for each
     entry;
   - a `.zip`/`.prop` round trip with a reserved test key: `PUT` both
     (`Content-Type: application/zip` for the zip), `GET` both byte-exact,
     `DELETE` both, then `GET` → `404`;
   - a missing `.zip` returns a real `404`, not `200` with an HTML page;
   - no redirect on `zotero/` (Zotero does not re-send WebDAV bodies across
     redirects).
3. Writes only `zotero-test-file.prop` (Zotero's own test file) and the
   reserved key pair `ZODAV0TS.zip`/`.prop` (`0` is outside Zotero's key
   alphabet, so it can never collide with a real attachment), and deletes
   them at the end, including after a failure part-way. `--read-only` skips
   every writing step.
4. Known-provider hints: when the URL host matches a known provider
   (Nextcloud/ownCloud `remote.php`, Synology, Koofr, pCloud, Jianguoyun,
   4shared, Box) and a step fails, print that provider's documented URL
   form or quirk.

### `integrity <url-or-dir> [--sample PERCENT]`

Never writes.

- each `.prop` parses as `<properties version="1">` with a valid `mtime`
  (as Zotero accepts it) and a 32-hex `hash` (flags `hash=undefined`,
  zotero/zotero#3573);
- each `.prop` has a `.zip` and each `.zip` has a `.prop`;
- each `.zip` is non-empty, opens, and passes `zipfile.testzip()`;
- when the zip holds exactly one file, `.prop` hash == MD5 of that file;
- names other than `<KEY>.zip`, `<KEY>.prop`, `lastsync`, `lastsync.txt`
  are listed as unexpected (info, not error).

Over a URL: one `PROPFIND Depth: 1`, one `GET` per file. `--sample` checks
a random subset of attachments.

### `repair <url-or-dir> [--apply] [--quarantine DIR]`

Dry run by default: prints the planned actions and exits. With `--apply`:

| Finding | Action |
|---|---|
| `.zip` with exactly one file and no `.prop` | write a `.prop` (hash = MD5 of the file, mtime = the zip entry's timestamp in ms) |
| `.prop` with an invalid `hash` or `mtime`, zip with exactly one file | rewrite the `.prop` (keep a valid mtime, else the entry's timestamp) |
| `.prop` without a `.zip` | copy to the quarantine directory, then delete from the store |
| corrupt zip, hash mismatch, multi-file zip without `.prop` | report only - Zotero re-uploads from the computer that has the file after "Reset File Sync History" |

Quarantine is always a local directory (default
`./zodav-quarantine-<UTC timestamp>/`), so repair works over WebDAV
without `MOVE`. Nothing is deleted unless its copy was written and
re-read byte-exact first.

### `watch <dir> [--every HOURS]` (container mode)

Runs `integrity` on the local store each interval (default 24 h), then:

- **mass-deletion alarm** - compares the attachment count with the previous
  run (stored in the reports volume); error when it dropped by more than
  `ZODAV_DELETE_ALERT_PERCENT` (default 10) and by at least 5 attachments. The baseline
  is the highest count seen and does not drop until the owner accepts the new
  count with `./zodav accept-deletions` (`watch --accept-current-count`);
- **idle store** - warning when the newest `.prop` is older than
  `ZODAV_IDLE_DAYS` (default 30): no client has uploaded anything;
- **free space** - error when free space on the data volume is below
  `ZODAV_MIN_FREE_GB` (default 2) or 5 %;
- **backup age** - error when `/backup-state/last-success` is older than
  26 hours; warning when it does not exist (backups never ran or are off).

Each run writes `latest.json` and `latest.html`, prints one summary line,
and, when there is any error or warning, sends an alert to every configured
channel: a webhook (`ZODAV_ALERT_URL`; Slack and Discord get their JSON
shape, anything else, such as ntfy, gets a plain-text body), a Telegram bot
(`ZODAV_TELEGRAM_BOT_TOKEN` + `ZODAV_TELEGRAM_CHAT_ID`, plain text, cut to
4096 characters) and email (`ZODAV_SMTP_*`, TLS verified; `none` security
logs a warning). A failure on one channel is logged without secrets and does
not affect the others. A half-set Telegram or SMTP configuration is an
invalid setting: exit 2. One "recovered" message is sent on the first
clean run after a failing one.

### Output, exit codes, privacy

- Default: one line per finding grouped by severity (error, warning, info),
  then a summary line. Each error says what is wrong, what Zotero will do
  because of it, and what to do.
- `--json`: the same as a JSON document with stable field names.
- `--html FILE`: a single self-contained HTML report (inline CSS, no
  scripts, no external requests), readable in light and dark mode, suitable
  for attaching to a forum post.
- Exit `0` no errors, `1` errors found, `2` could not run.
- Passwords and `Authorization` headers never appear in any output; URLs are
  printed without credentials.

## Security requirements

- No host port published; only the sidecar is on the tailnet; tailnet access
  limited by ACL/grants (documented for both Tailscale and Headscale).
- Every request under `/zotero/` requires a valid user. Methods other than
  OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL are refused, including
  LOCK, UNLOCK, PROPPATCH, MOVE, COPY, TRACE.
- `DavDepthInfinity Off`. `LimitRequestBody` 1 GiB. `ServerTokens Prod`,
  `ServerSignature Off`, `TraceEnable Off`, `Options None`.
- Passwords: at least 24 characters, `[A-Za-z0-9_-]` only (safe in `.env`
  and compose interpolation), hashed with bcrypt into tmpfs at start; never
  written to the data volume or logs. `./zodav setup` generates them.
- Containers: non-root where the image allows, `read_only` root filesystem
  where possible, `cap_drop: [ALL]`, `no-new-privileges`, memory limits,
  images pinned by tag and digest, Renovate keeps pins current, Trivy scans
  the built image in CI.
- Access log to stdout: client, user, method, path, status, bytes, duration.
  No bodies, no auth headers.

## Data safety requirements

- An interrupted or failed `PUT` never destroys the previous version.
- A full disk makes `PUT` fail with `507` and keeps the previous version.
- Restarts and redeploys keep all data (named volume `zodav-data`).
- Backups: encrypted (restic), off-host, pruned, verified weekly.
- `./zodav restore-drill` restores the latest snapshot into a scratch volume
  and runs `integrity` on it.

## User experience

- `./zodav` (POSIX sh) is the only command a user needs:
  `setup` (interactive; writes `.env` with generated passwords, mode 600,
  refuses to overwrite), `start`, `stop`, `status`, `settings` (prints the
  exact Zotero settings - protocol, URL from the tailnet, username;
  password only with `--show-password`), `check` (conformance + integrity
  now), `report` (copies the latest HTML report to the current directory),
  `repair` (dry run; `--apply` to act), `accept-deletions`, `restore-drill`,
  `logs`.
- `.env.example` documents every variable in plain language; only three are
  needed for a first start: `ZODAV_PASSWORD`, `TS_AUTHKEY`, and (optionally)
  `ZODAV_HEADSCALE_URL`.
- Works unchanged as a Dokploy / Portainer / Coolify compose app: the same
  variables go into the platform's environment form.

## Compatibility matrix

`compat/` holds a compose file that starts several WebDAV servers on
loopback ports (ZoDAV, rclone `serve webdav`, hacdias/webdav, nginx with
its built-in DAV module, Nextcloud) and a script that runs `conformance`
against each and writes a Markdown table. CI runs it on every push and
writes the table to the job summary; the README carries the latest table
with its date.

## Deliverables

`compose.yaml`, `.env.example`, `zodav`, `webdav/` (Dockerfile, httpd.conf,
entrypoint), `audit/zodav_audit.py` (+ Dockerfile), `backup/run.sh`,
`compat/`, `tests/` (pytest), `.github/workflows/ci.yml`, `renovate.json`,
`pyproject.toml` (test config and the `zodav-audit` console script),
`README.md` (with table of contents, quick start, Zotero setup,
architecture, every command, troubleshooting, compatibility table, to-do
list), `SECURITY.md`, `LICENSE` (AGPL-3.0).

## Acceptance

- `pytest` passes against `docker-compose.test.yml`: access rules, the full
  Verify sequence, a `K.zip`/`K.prop` round trip, interrupted `PUT`,
  disk-full `507`, backup + restore drill, compose hardening checks.
- `zodav-audit` tests: conformance passes against the test stack and, against
  a deliberately broken server, reports `NOT_DAV` and
  `NONEXISTENT_FILE_NOT_MISSING`; integrity over URL and over a directory
  report the same findings on a fixture store with one of each defect;
  repair dry run changes nothing, `--apply` fixes the fixable defects and
  quarantines the orphan; watch raises the mass-deletion, idle, free-space
  and backup-age findings and posts one alert to a local webhook receiver.
- `shellcheck` clean on every shell script.
- No secrets, personal data or private infrastructure names anywhere in the
  repository or its history.
