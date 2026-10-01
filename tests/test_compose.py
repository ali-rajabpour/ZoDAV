import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose"),
]

DUMMY = {
    "ZODAV_PASSWORD": "a" * 32,
    "TS_AUTHKEY": "tskey-auth-dummy",
}


def compose_config(env=None):
    # --env-file /dev/null keeps a developer's own .env out of the test.
    full = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    full.update(env or {})
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "compose.yaml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=full,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def cfg():
    r = compose_config(DUMMY)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_fails_without_required_vars_and_points_at_setup():
    r = compose_config()
    assert r.returncode != 0
    assert "./zodav setup" in r.stderr


def test_backup_vars_not_required(cfg):
    assert "backup" not in cfg["services"]  # profile is off, file still validates


def test_services_and_backup_profile():
    r = compose_config({**DUMMY, "COMPOSE_PROFILES": "backup"})
    assert r.returncode == 0, r.stderr
    services = json.loads(r.stdout)["services"]
    assert set(services) == {"webdav", "tailscale", "audit", "backup"}
    assert services["backup"]["profiles"] == ["backup"]


def test_no_ports_or_proxy_labels(cfg):
    for name, svc in cfg["services"].items():
        assert "ports" not in svc, name
        assert not any("traefik" in k.lower() for k in svc.get("labels", {})), name


def all_services():
    r = compose_config({**DUMMY, "COMPOSE_PROFILES": "backup"})
    return json.loads(r.stdout)["services"]


def test_hardening_on_every_service():
    for name, svc in all_services().items():
        assert svc["restart"] == "unless-stopped", name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cap_drop"] == ["ALL"], name
        assert svc["read_only"] is True, name
        assert int(svc["mem_limit"]) > 0, name


def volumes(svc):
    return {v["target"]: v for v in svc.get("volumes", [])}


def test_data_mounts():
    s = all_services()
    assert not volumes(s["webdav"])["/data"].get("read_only")
    assert volumes(s["audit"])["/data"]["read_only"] is True
    assert volumes(s["backup"])["/data"]["read_only"] is True
    assert volumes(s["audit"])["/reports"].get("read_only") is not True
    assert volumes(s["audit"])["/backup-state"]["read_only"] is True
    assert volumes(s["backup"])["/state"].get("read_only") is not True


def test_backup_state_is_a_named_volume_not_tmpfs():
    b = all_services()["backup"]
    assert volumes(b)["/state"]["type"] == "volume"
    assert not any(str(t).startswith("/state") for t in b.get("tmpfs", []))


def test_shared_network_only_webdav(cfg):
    assert cfg["networks"]["shared"]["name"] == "zodav"
    joined = [n for n, s in cfg["services"].items() if "shared" in s["networks"]]
    assert joined == ["webdav"]


def test_init_process_on_long_running_scripts():
    s = all_services()
    assert s["audit"]["init"] is True and s["backup"]["init"] is True


def test_no_unsupported_backup_credentials_passed():
    env = all_services()["backup"]["environment"]
    assert not any(k.startswith("GOOGLE_") for k in env)
