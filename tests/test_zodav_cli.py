import os
import pty
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUBCOMMANDS = [
    "setup",
    "start",
    "stop",
    "status",
    "settings",
    "check",
    "report",
    "repair",
    "accept-deletions",
    "restore-drill",
    "logs",
    "help",
]

CANNED = """{
  "Version": "1.102.5",
  "Self": {
    "ID": "n1",
    "DNSName": "zodav.tail1234.ts.net.",
    "HostName": "zodav"
  },
  "Peer": {"x": {"DNSName": "other.tail1234.ts.net."}}
}
"""


@pytest.fixture
def repo(tmp_path):
    shutil.copy(ROOT / "zodav", tmp_path / "zodav")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    (tmp_path / "zodav").chmod(0o755)
    # Stub docker: answers just enough of `docker compose` for the script.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "canned.json").write_text(CANNED)
    stub = bin_dir / "docker"
    stub.write_text(f"""#!/bin/sh
echo "$*" >> {tmp_path}/calls.log
case "$*" in
  "compose version"|info) exit 0 ;;
  "compose ps --status running -q webdav") echo abc123 ;;
  "compose exec -T tailscale tailscale status --json") cat {tmp_path}/canned.json ;;
  "compose cp audit:/reports/latest.html "*)
    if [ -f {tmp_path}/cp_error.txt ]; then cat {tmp_path}/cp_error.txt >&2; exit 1; fi
    echo report > "$4" ;;
  "compose up "*|"compose run "*) ;;
  *) echo "stub docker: unexpected: $*" >&2; exit 1 ;;
esac
""")
    stub.chmod(0o755)
    return tmp_path


def run(repo, *args, stdin=""):
    env = {"PATH": f"{repo}/bin:{os.environ['PATH']}", "HOME": str(repo)}
    return subprocess.run(
        [str(repo / "zodav"), *args], cwd=repo, env=env, input=stdin, capture_output=True, text=True
    )


def env_file(repo):
    out = {}
    for line in (repo / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_setup_writes_private_env_and_refuses_second_run(repo):
    r = run(
        repo,
        "setup",
        stdin="tskey-auth-abc123\n\nhttps://ntfy.sh/topic\ns3:s3.example.com/bucket\n",
    )
    assert r.returncode == 0, r.stderr
    env = repo / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    vals = env_file(repo)
    assert vals["TS_AUTHKEY"] == "tskey-auth-abc123"
    assert vals["ZODAV_ALERT_URL"] == "https://ntfy.sh/topic"
    assert vals["COMPOSE_PROFILES"] == "backup"
    assert vals["RESTIC_REPOSITORY"] == "s3:s3.example.com/bucket"
    for k in ("ZODAV_PASSWORD", "RESTIC_PASSWORD"):
        assert len(vals[k]) == 32 and vals[k].isalnum()
    assert vals["ZODAV_PASSWORD"] != vals["RESTIC_PASSWORD"]
    assert "password manager" in r.stdout

    before = env.read_text()
    r2 = run(repo, "setup", stdin="other\n\n\n\n")
    assert r2.returncode != 0
    assert "already exists" in r2.stderr
    assert env.read_text() == before


def test_setup_without_backup_leaves_it_off(repo):
    assert run(repo, "setup", stdin="tskey-auth-abc\n\n\n\n").returncode == 0
    vals = env_file(repo)
    assert "COMPOSE_PROFILES" not in vals and "RESTIC_PASSWORD" not in vals


def test_setup_requires_key(repo):
    r = run(repo, "setup", stdin="\n")
    assert r.returncode != 0
    assert not (repo / ".env").exists()


def test_help_lists_every_subcommand(repo):
    out = run(repo, "help").stdout
    for c in SUBCOMMANDS:
        assert c in out


def test_settings_hides_password_by_default(repo):
    run(repo, "setup", stdin="tskey-auth-abc\n\n\n\n")
    pw = env_file(repo)["ZODAV_PASSWORD"]
    r = run(repo, "settings")
    assert r.returncode == 0, r.stderr
    assert "URL:       zodav.tail1234.ts.net\n" in r.stdout
    assert "Protocol:  http\n" in r.stdout
    assert "Username:  zotero" in r.stdout
    assert pw not in r.stdout
    assert "--show-password" in r.stdout
    assert f"Password:  {pw}" in run(repo, "settings", "--show-password").stdout


def test_missing_env_gives_hint(repo):
    r = run(repo, "settings")
    assert r.returncode != 0
    assert "./zodav setup" in r.stderr


def calls(repo):
    return (repo / "calls.log").read_text().splitlines()


def test_envval_strips_quotes_cr_and_inline_comments(repo):
    (repo / ".env").write_text("ZODAV_USERNAME=alice  # who\r\nZODAV_PASSWORD='pw # kept'\r\n")
    out = run(repo, "settings", "--show-password").stdout
    assert "Username:  alice\n" in out
    assert "Password:  pw # kept\n" in out
    (repo / ".env").write_text('ZODAV_USERNAME="bob"\nZODAV_PASSWORD=x#y\n')
    out = run(repo, "settings", "--show-password").stdout
    assert "Username:  bob\n" in out and "Password:  x#y\n" in out


def test_setup_does_not_follow_env_symlink_and_leaves_no_temp_files(repo):
    (repo / "elsewhere").write_text("keep")
    (repo / ".env").symlink_to(repo / "elsewhere")
    r = run(repo, "setup", stdin="tskey-auth-abc\n\n\n\n")
    assert r.returncode != 0 and "already exists" in r.stderr
    assert (repo / "elsewhere").read_text() == "keep"
    (repo / ".env").unlink()
    assert run(repo, "setup", stdin="tskey-auth-abc\n\n\n\n").returncode == 0
    assert [p.name for p in repo.glob(".env.*") if p.name != ".env.example"] == []


def test_setup_tells_user_to_revoke_the_auth_key(repo):
    r = run(repo, "setup", stdin="tskey-auth-abc\n\n\n\n")
    assert "revoke or expire the" in r.stdout


def test_setup_does_not_echo_the_key_at_a_terminal(repo):
    master, slave = pty.openpty()
    env = {"PATH": f"{repo}/bin:{os.environ['PATH']}", "HOME": str(repo)}
    proc = subprocess.Popen(
        [str(repo / "zodav"), "setup"],
        cwd=repo,
        env=env,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(slave)
    key = "tskey-auth-secretvalue"
    seen = b""

    def drain(seconds):
        nonlocal seen
        end = time.time() + seconds
        while time.time() < end:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    seen += os.read(master, 4096)
                except OSError:
                    return

    drain(1.0)  # let the script reach the key prompt and turn echo off
    os.write(master, (key + "\n").encode())
    drain(1.0)
    for _ in range(3):
        os.write(master, b"\n")
        drain(0.3)
    proc.communicate(timeout=30)
    os.close(master)
    assert proc.returncode == 0
    assert key.encode() not in seen
    assert env_file(repo)["TS_AUTHKEY"] == key


def test_start_tightens_env_and_does_not_wait_on_backup(repo):
    (repo / ".env").write_text("ZODAV_PASSWORD=x\n")
    (repo / ".env").chmod(0o644)
    r = run(repo, "start")
    assert r.returncode == 0, r.stderr
    assert "readable only by you" in r.stdout
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600
    c = calls(repo)
    assert "compose up -d --build" in c
    assert "compose up -d --wait webdav audit" in c
    assert "compose up -d --build --wait" not in c


def test_report_distinguishes_missing_report_from_docker_errors(repo):
    (repo / ".env").write_text("ZODAV_PASSWORD=x\n")
    r = run(repo, "report")
    assert r.returncode == 0, r.stderr
    (out,) = repo.glob("zodav-report-*.html")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    out.unlink()

    (repo / "cp_error.txt").write_text(
        "Error: Could not find the file /reports/latest.html in container x"
    )
    r = run(repo, "report")
    assert r.returncode != 0 and "no report yet" in r.stderr

    (repo / "cp_error.txt").write_text("Error response from daemon: permission denied")
    r = run(repo, "report")
    assert r.returncode != 0 and "permission denied" in r.stderr
    assert "no report yet" not in r.stderr
    assert not list(repo.glob("zodav-report-*.html"))


def test_accept_deletions_runs_audit_watch_with_the_flag(repo):
    (repo / ".env").write_text("ZODAV_PASSWORD=x\n")
    r = run(repo, "accept-deletions")
    assert r.returncode == 0, r.stderr
    assert (
        "compose run --rm -T audit watch /data/zotero --reports /reports "
        "--backup-state /backup-state --accept-current-count"
    ) in calls(repo)
