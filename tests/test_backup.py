import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest
from davclient import request
from fakedav import make_zip, prop

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.docker
COMPOSE = ["docker", "compose", "-f", "docker-compose.test.yml"]


def run(*args, timeout=180):
    return subprocess.run(args, cwd=ROOT, capture_output=True, timeout=timeout)


def logs(service):
    out = run(*COMPOSE, "logs", "--no-color", service)
    return (out.stdout + out.stderr).decode(errors="replace")


def health(service):
    out = run(
        "docker", "inspect", "--format", "{{.State.Health.Status}}", f"zodav-test-{service}-1"
    )
    return out.stdout.decode().strip()


def wait_for(cond, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(1)
    return False


def snapshot_file(name):
    out = run(
        *COMPOSE,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "restic",
        "backup",
        "dump",
        "latest",
        f"/data/zotero/{name}",
        "--host",
        "zodav",
    )
    return out.stdout if out.returncode == 0 else None


def test_backup_runs_repeatedly_and_is_healthy():
    assert wait_for(lambda: logs("backup").count("backup ok") >= 2)
    assert wait_for(lambda: health("backup") == "healthy")


def test_restore_drill_restores_file_written_just_before():
    content = b"restore drill payload"
    zbytes = make_zip({"f.txt": content})
    md5 = hashlib.md5(content).hexdigest()
    assert request("PUT", "/zotero/DRILL222.zip", body=zbytes)[0] in (201, 204)
    assert request("PUT", "/zotero/DRILL222.prop", body=prop(md5=md5))[0] in (201, 204)
    try:
        assert wait_for(lambda: snapshot_file("DRILL222.zip") == zbytes), (
            "file never reached a snapshot"
        )
        out = run("backup/restore-drill.sh", "docker-compose.test.yml", timeout=300)
        assert out.returncode == 0, out.stdout + out.stderr
        assert b"restore drill ok" in out.stdout
    finally:
        for ext in ("zip", "prop"):
            request("DELETE", f"/zotero/DRILL222.{ext}")
    leftover = run("docker", "volume", "ls", "-q", "--filter", "name=zodav-restore-drill")
    assert leftover.stdout.decode().strip() == ""


@pytest.mark.parametrize(
    "password, reason",
    [
        ("short-password", "shorter than 24"),
        ("CHANGE_ME_32_RANDOM_LETTERS_AND_DIGITS", "CHANGE_ME placeholder"),
    ],
)
def test_weak_restic_password_is_refused(password, reason):
    name = "zodav-test-weak-password"
    run("docker", "rm", "-f", name)
    started = run(
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "BACKUP_INTERVAL_SECONDS=2",
        "-e",
        "RESTIC_REPOSITORY=/repo",
        "-e",
        f"RESTIC_PASSWORD={password}",
        "zodav-backup:test",
    )
    assert started.returncode == 0, started.stderr.decode()
    try:
        assert wait_for(lambda: logs_of(name).count("BACKUP FAILED: RESTIC_PASSWORD") >= 2, 30)
        assert reason in logs_of(name)
        assert (
            run("docker", "inspect", "--format", "{{.State.Running}}", name).stdout.decode().strip()
            == "true"
        )
    finally:
        run("docker", "rm", "-f", name)


def test_empty_store_skips_backup_and_never_counts_as_success():
    name = "zodav-test-empty-store"
    run("docker", "rm", "-f", name)
    started = run(
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "BACKUP_INTERVAL_SECONDS=2",
        "-e",
        "RESTIC_REPOSITORY=/repo",
        "-e",
        "BACKUP_SOURCE=/empty",
        "-e",
        "RESTIC_PASSWORD=test-restic-password-0123456789",
        "--tmpfs",
        "/empty",
        "zodav-backup:test",
    )
    assert started.returncode == 0, started.stderr.decode()
    try:
        assert wait_for(lambda: logs_of(name).count("BACKUP SKIPPED: the store is empty") >= 2, 30)
        assert "backup ok" not in logs_of(name)
        assert run("docker", "exec", name, "test", "-e", "/state/last-success").returncode != 0
    finally:
        run("docker", "rm", "-f", name)


def test_sigterm_stops_backup_promptly():
    name = "zodav-test-sigterm"
    run("docker", "rm", "-f", name)
    started = run(
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "BACKUP_INTERVAL_SECONDS=3600",
        "zodav-backup:test",
    )
    assert started.returncode == 0, started.stderr.decode()
    try:
        assert wait_for(lambda: "BACKUP FAILED" in logs_of(name), 30)
        t = time.time()
        assert run("docker", "stop", "-t", "20", name).returncode == 0
        assert time.time() - t < 5
    finally:
        run("docker", "rm", "-f", name)


def test_unreachable_repository_logs_failure_and_turns_unhealthy():
    up = run(*COMPOSE, "--profile", "broken", "up", "-d", "backup-broken")
    assert up.returncode == 0, up.stderr.decode()
    assert wait_for(lambda: "BACKUP FAILED" in logs("backup-broken"))
    assert wait_for(lambda: health("backup-broken") == "unhealthy")


@pytest.mark.parametrize(
    "var, env",
    [
        ("RESTIC_REPOSITORY", []),
        ("RESTIC_PASSWORD", ["-e", "RESTIC_REPOSITORY=/repo"]),
    ],
)
def test_missing_config_is_logged_and_container_stays_alive(var, env):
    name = f"zodav-test-misconfig-{var.lower()}"
    run("docker", "rm", "-f", name)
    started = run(
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "BACKUP_INTERVAL_SECONDS=2",
        *env,
        "zodav-backup:test",
    )
    assert started.returncode == 0, started.stderr.decode()
    try:
        assert wait_for(lambda: logs_of(name).count(f"BACKUP FAILED: {var} is not set") >= 2, 30)
        state = run("docker", "inspect", "--format", "{{.State.Running}}", name)
        assert state.stdout.decode().strip() == "true"
    finally:
        run("docker", "rm", "-f", name)


def logs_of(container):
    out = run("docker", "logs", container)
    return (out.stdout + out.stderr).decode(errors="replace")


def test_restore_drill_fails_when_snapshot_has_no_attachments(tmp_path):
    # Stub docker: every step succeeds except the integrity container, which
    # reports "clean but zero attachments" (exit 3).
    stub = tmp_path / "docker"
    stub.write_text('#!/bin/sh\ncase "$*" in *"python -c"*) exit 3 ;; esac\nexit 0\n')
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    out = subprocess.run(
        ["sh", "backup/restore-drill.sh", "docker-compose.test.yml"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode != 0
    assert "the latest snapshot contains no attachments" in out.stdout
