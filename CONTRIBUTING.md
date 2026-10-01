# Contributing to ZoDAV

Thanks for helping. Bug reports, compatibility reports, docs fixes and code are all welcome. Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first. Security problems go through [private vulnerability reporting](https://github.com/ali-rajabpour/ZoDAV/security/advisories/new), not public issues; see [SECURITY.md](SECURITY.md).

## Setup

You need Docker with Compose v2 and [uv](https://docs.astral.sh/uv/). Python 3.10 or newer is enough for the audit tool itself.

```bash
git clone https://github.com/ali-rajabpour/ZoDAV.git
cd ZoDAV
```

## Running the tests

```bash
docker compose -f docker-compose.test.yml up -d --build --wait
uv run --no-project --with pytest==9.1.1 pytest -q
sh tests/smoke.sh
docker compose -f docker-compose.test.yml --profile broken down -v
```

Lint:

```bash
shellcheck zodav webdav/entrypoint.sh backup/*.sh compat/run.sh
uvx ruff check .
```

CI also runs hadolint, actionlint, CodeQL and Trivy; see "Development and tests" in the [README](README.md).

## Project layout

| Path | Contents |
|---|---|
| `webdav/` | Apache httpd WebDAV server image and entrypoint |
| `audit/zodav_audit.py` | The whole `zodav-audit` tool, one file |
| `backup/` | restic backup image and restore drill |
| `compat/` | Compatibility matrix runner and its server configs |
| `tests/` | pytest suite, fake WebDAV server, smoke test |
| `docs/SPEC.md` | Design spec |
| `zodav`, `compose.yaml` | Control script and deployment stack |

## Rules

- `zodav-audit` is standard library only, and the project adds no new runtime dependencies.
- Every behaviour change comes with a test. Never weaken, skip or delete a test to make a change pass.
- Claims about the Zotero protocol must be sourced from Zotero's own client code (`chrome/content/zotero/xpcom/storage/webdav.js` in [zotero/zotero](https://github.com/zotero/zotero)). Cite the behaviour you checked in the PR.
- Keep shell scripts passing `shellcheck` and Python passing `ruff`.
- Update the README and `docs/SPEC.md` when behaviour changes, and add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).

## Adding a provider hint

Provider hints are the `PROVIDER_HINTS` list in `audit/zodav_audit.py`: a regex matched against the server URL and the advice shown to the user. Add an entry with a short, accurate instruction (URL form, app password, port), then add a sample URL to `test_provider_hints_match` in `tests/test_audit_conformance.py`.

## Submitting a compatibility report

Run `zodav-audit conformance --json --read-only` against your server and open a [compatibility report](https://github.com/ali-rajabpour/ZoDAV/issues/new?template=compatibility_report.yml). Remove URLs, usernames, passwords and tokens from the output first. To add a server to the matrix itself, add a service under `compat/` and wire it into `compat/run.sh`.

## Commits and pull requests

- Keep each pull request focused on one change, and make it against `main`.
- Write short, specific commit messages in the imperative: "Fix redirect check for trailing slash".
- Fill in the pull request template: summary, tests run, checklist.
- Never commit secrets, `.env` files, screenshots or recordings.

## License

ZoDAV is licensed under [AGPL-3.0-only](LICENSE). By submitting a contribution you agree it is licensed under the same terms. There is no CLA. Adding a `Signed-off-by:` line (`git commit -s`) is welcome but not required.
