# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.1] - 2026-10-01

### Changed

- `TS_AUTHKEY` is only needed for the first start. The login is kept in the `tailscale-state` volume, so a single-use pre-auth key can be removed from the environment afterwards.
- The Tailscale sidecar runs with `--accept-dns=false`, so name lookups such as `webdav` always use Docker's resolver.
- The Headscale guide now uses Headscale 0.29 `grants` with an empty `tagOwners` entry and a tagged single-use pre-auth key, matching hubs where only the administrator can tag devices.

## [1.0.0] - 2026-10-01

First release.

### Added

- Private Zotero WebDAV server: Apache httpd with `mod_dav`, reachable only over a Tailscale or Headscale tailnet, with no published ports. Only the methods Zotero uses are allowed, and every request under `/zotero/` needs a bcrypt-hashed password.
- `./zodav` control script for setup, status, checks, backups and updates, with a deployment `compose.yaml` and `.env` template. The server refuses to start with the placeholder password.
- `zodav-audit`, a standard-library-only Python 3.10+ checker for any Zotero WebDAV server:
  - `conformance`: the steps of Zotero's Verify Server plus the checks it skips, with each failure mapped to the Zotero error, likely cause and fix, and URL hints for Nextcloud, Synology, Koofr, pCloud, Jianguoyun, 4shared and Box.
  - `integrity`: `.zip` / `.prop` consistency and streaming zip checks on a URL or a local folder.
  - `repair`: dry run by default, read-back verification and a quarantine for removed files.
  - `watch`: container mode with a daily self-check, sticky mass-deletion baseline with accept-deletions, idle-store, low-disk and backup-staleness alarms.
  - Self-contained HTML audit report.
- Alert channels: generic webhook, ntfy, Slack, Discord, Telegram and email.
- Encrypted off-host backups with restic, automatic pruning, weekly verification and a one-command restore drill.
- Compatibility matrix (`compat/run.sh`) that audits popular WebDAV servers and writes `compat/RESULTS.md`.
- Hardened containers: non-root where the image allows, read-only root filesystems, all capabilities dropped, no privilege escalation, images pinned by digest.
- Test suite (pytest with a Docker test stack), GitHub Actions CI with shellcheck, compose validation, the compatibility matrix and Trivy image scans, and Renovate for digest updates.
- Design spec, README and security policy.

[Unreleased]: https://github.com/ali-rajabpour/ZoDAV/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/ali-rajabpour/ZoDAV/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/ali-rajabpour/ZoDAV/releases/tag/v1.0.0
