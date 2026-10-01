# Security

This document describes how ZoDAV is meant to be secured, what each secret allows, and how to report a problem.

## Threat model

ZoDAV stores your Zotero attachment files. Its main protection is that it is **not on the public internet**.

- No host port is published. `compose.yaml` has no `ports:` entries, no reverse-proxy labels and no public DNS name.
- The only way in from outside the host is the Tailscale (or Headscale) sidecar, which listens on the tailnet only and forwards TCP port 80 to the WebDAV container.
- Tailnet traffic is encrypted end to end by WireGuard. That is why Zotero can use plain `http` to the tailnet address.
- Access inside the tailnet is limited by your ACL or grants. The documented rule lets your own users' devices reach the ZoDAV machine on `tcp:80` and nothing else.

### What someone on your tailnet can and cannot do

Given the ACL in the README (`autogroup:member` to `tag:zodav` on `tcp:80`):

- A device of a member of your tailnet can open a connection to ZoDAV on port 80. It still needs the user name and password. Without them it can only read `/healthz`, which returns `ok`.
- A device that is not covered by the rule (for example a shared-in guest device, or a tagged server) cannot connect at all.
- ZoDAV cannot start connections to your other tailnet devices, because the rule only allows traffic towards it.
- With a valid login, a client can read, write and delete files in `/zotero/`. That is all it can do. `LOCK`, `UNLOCK`, `PROPPATCH`, `MOVE`, `COPY` and `TRACE` are refused. Directory listings with infinite depth are off. Uploads are limited to 1 GiB per request. Nothing outside the `zotero/` folder is served.

Not in scope, or not protected against:

- **Password guessing by a tailnet member.** There is no rate limiting or lockout yet (see the to-do list in the README). Passwords are generated with 32 random characters, which makes guessing impractical.
- **Anyone who controls the Docker host.** Root on the server, or membership of the `docker` group, can read `.env`, the volumes and container environments.
- **Containers you join to the `zodav` Docker network.** They can reach `webdav:8080` and are only stopped by the password. Only join containers you trust.
- **A compromised Tailscale account.** Whoever controls your tailnet policy can change who reaches ZoDAV. Protect that account with strong authentication.
- **Public exposure.** Do not add published ports, tunnels or a reverse proxy in front of ZoDAV. The design assumes none exist.

## What each credential grants

| Credential | Where it is set | What it grants | If it leaks |
|---|---|---|---|
| `ZODAV_PASSWORD` (with `ZODAV_USERNAME`) | `.env` | Full read, write and delete access to every attachment file, from a device that can reach the server on the tailnet. | Change it in `.env` and run `./zodav start`. Enter the new one in Zotero. Also used by the `audit` container to run its checks. |
| `ZODAV_SERVICE_PASSWORD` (with `ZODAV_SERVICE_USERNAME`) | `.env` | The same access as the main user. A separate login so it can be rotated alone and the access log shows who wrote what. | Change it and restart. The main user is unaffected. |
| `TS_AUTHKEY` | `.env` | Lets a machine join your tailnet as `tag:zodav` (Tailscale) or register on your Headscale server. The key is only used for the first login (`TS_AUTH_ONCE` and the stored node identity), so revoke or expire it in the admin console once ZoDAV appears there. A one-off key is used up when ZoDAV first joins; a reusable key can be used again until you revoke it. | Revoke the key in the admin console (or expire it in Headscale). If it was reusable, also check the list of machines for ones you do not recognise. |
| Tailscale node identity | Docker volume `tailscale-state` | The machine's login to your tailnet. | Remove the machine in the admin console. |
| `RESTIC_PASSWORD` | `.env` | Decrypts the backups. With repository access, it exposes all backed-up attachments. Losing it makes backups unreadable. | Create a new repository with a new password and take a fresh backup. Old snapshots stay readable by whoever has the old password. |
| Cloud keys (`AWS_*`, `B2_*`, `AZURE_*`, `RESTIC_REST_*`) | `.env` | Access to the backup repository: reading encrypted data and, depending on the key, deleting it. They do not give access to the live server. | Rotate the key at the provider. Use a key limited to the one bucket. |
| `ZODAV_ALERT_URL` | `.env` | Posting messages to your alert channel. Alerts contain file names and counts, not file content. | Create a new webhook or topic and replace the value. |
| `ZODAV_TELEGRAM_BOT_TOKEN` | `.env` | Sending messages as your bot. | Revoke it with `/revoke` in `@BotFather` and put the new token in `.env`. |
| `ZODAV_SMTP_PASSWORD` | `.env` | Sending email through your SMTP account (use an app password, not your main one). | Delete the app password at your mail provider and create a new one. |

## Where secrets live

- **`.env`** on the server, created by `./zodav setup` with mode 600 (readable only by your user). It is listed in `.gitignore`. Never commit it or paste it into an issue.
- **Your hosting panel's environment form** when you deploy with Dokploy, Portainer or Coolify. Treat panel access as access to the secrets.
- **Inside the containers:** passed as environment variables. They are visible to anyone who can run `docker inspect` on the host. The `webdav` container hashes the passwords with bcrypt (cost 10) into a file on a memory-only filesystem (`/run/zodav`, tmpfs) when it starts, and removes the plain-text variables from the shell before starting Apache. Only hashes are on that tmpfs, and they are never written to the data volume or to the logs. The `audit` container receives the main password as `ZODAV_AUDIT_PASSWORD`, plus the alert settings (webhook URL, Telegram token, SMTP password); no other container gets the alert secrets.
- **Not stored anywhere:** the audit tool never writes the password to disk. Reports and alerts do not contain passwords or `Authorization` headers.
- **The `.env` placeholder is not a secret.** `.env.example` contains `CHANGE_ME` values. Replace them, or use `./zodav setup`; the server refuses to start with a `CHANGE_ME` password.


## Container hardening

All four containers share these settings (`compose.yaml`):

- `read_only: true`: the root filesystem is read-only. Writable places are the data volume and small tmpfs mounts.
- `cap_drop: [ALL]`: no Linux capabilities.
- `no-new-privileges: true`: processes cannot gain privileges.
- Memory limits (512 MB, 256 MB, 256 MB, 512 MB).
- Non-root users: `webdav` runs as `www-data`, `audit` as `nobody`, `backup` as `nobody` (65534). The Tailscale sidecar uses userspace networking, so it needs neither `NET_ADMIN` nor `/dev/net/tun`.
- The `audit` and `backup` containers mount the data volume read-only. A bug in either cannot modify your files.
- Base images are pinned by version and digest: Apache httpd, Tailscale, restic and Python.
- [Renovate](https://docs.renovatebot.com) opens pull requests to keep those pins current, and CI scans the built `webdav`, `audit` and `backup` images with [Trivy](https://trivy.dev) and fails on fixable critical vulnerabilities.
- Apache runs a minimal configuration: only the modules it needs, `ServerTokens Prod`, `ServerSignature Off`, `TraceEnable Off`, `Options None`, a 1 GiB request limit and read timeouts. The access log goes to stdout and contains client, user, method, path, status, size and duration. No request bodies, no auth headers.

## Backup encryption

Backups are made with [restic](https://restic.net). Data is encrypted and authenticated on your server with a key derived from `RESTIC_PASSWORD` before it is sent anywhere, so the storage provider only sees encrypted blobs. Pruning keeps 7 daily, 4 weekly and 12 monthly snapshots, and a weekly check re-reads 5 percent of the stored data.

Be aware:

- The backup container holds both the repository credentials and `RESTIC_PASSWORD`, and can delete snapshots (it prunes them). A compromised ZoDAV server could therefore delete its own backups. Append-only backups are on the to-do list.
- The file names inside backups are encrypted too, but their number and size are not hidden.

## The audit tool's network behaviour

`zodav-audit` is a standalone script using only the Python standard library.

- It talks only to the URL you give it, plus the alert destinations in watch mode: `ZODAV_ALERT_URL`, the Telegram API and your SMTP server.
- **TLS certificates are always verified.** There is no option to disable verification.
- It does not follow redirects, so credentials are not sent to a different host. A redirect is reported as a finding.
- Credentials come from `ZODAV_AUDIT_PASSWORD` or a prompt, never from a command-line argument. They are not printed in text, JSON or HTML output, and URLs are shown without embedded credentials.
- Alert failures print the reason only, not the URL, because webhook URLs often carry a token. Telegram tokens and SMTP passwords are never printed. SMTP uses certificate-verified TLS unless you set `ZODAV_SMTP_SECURITY=none`, which logs a warning.
- `conformance` writes only `zotero-test-file.prop`, `ZODAV0TS.zip` and `ZODAV0TS.prop`, and removes them. `integrity` never writes. `repair` is a dry run unless `--apply` is given, and it copies a file to a local quarantine folder and verifies the copy before deleting anything.
- The HTML report has no scripts and makes no external requests.

## Reporting a vulnerability

Please report security problems privately, using GitHub's private vulnerability reporting: open the repository on GitHub, go to the **Security** tab, and choose **Report a vulnerability**.

Include what you found, how to reproduce it, and which version or commit you tested. Do not open a public issue for a vulnerability and do not post secrets, `.env` files or real hostnames. You will get a reply as soon as the maintainer can, and you will be told when a fix is released.
