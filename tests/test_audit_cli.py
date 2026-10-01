import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fakedav import PASSWORD, USER, build_clean_store, build_defect_store, fakedav  # noqa: F401
from zodav_audit import main

TOOL = Path(__file__).resolve().parent.parent / "audit" / "zodav_audit.py"


def run_cli(args, env_extra=None, stdin=subprocess.DEVNULL):
    env = {k: v for k, v in os.environ.items() if k != "ZODAV_AUDIT_PASSWORD"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=stdin,
        timeout=60,
    )


def test_exit_0_on_clean_directory(tmp_path, capsys):
    build_clean_store(tmp_path)
    assert main(["integrity", str(tmp_path)]) == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_exit_1_on_errors(tmp_path, capsys):
    build_defect_store(tmp_path)
    assert main(["integrity", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "ERRORS" in out and "What Zotero will do" in out and "What to do" in out


def test_exit_2_when_it_cannot_run(tmp_path, capsys):
    assert main(["integrity", str(tmp_path / "missing")]) == 2
    assert main(["integrity", str(tmp_path), "--sample", "0"]) == 2


def test_unreachable_server_exits_2(monkeypatch):
    monkeypatch.setenv("ZODAV_AUDIT_PASSWORD", "x")
    assert main(["conformance", "http://127.0.0.1:1/"]) == 2


def test_json_output_parses(tmp_path, capsys):
    build_defect_store(tmp_path)
    main(["integrity", str(tmp_path), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "integrity" and doc["ok"] is False
    assert {"severity", "code", "subject", "message", "effect", "fix"} <= set(doc["findings"][0])
    assert doc["summary"]["errors"] > 0


def test_html_report_written_and_escaped(tmp_path):
    build_defect_store(tmp_path / "z")
    out = tmp_path / "r.html"
    main(["integrity", str(tmp_path / "z"), "--html", str(out)])
    page = out.read_text()
    assert page.startswith("<!doctype html>") and "MISSING_PROP" in page and "<script" not in page


def test_missing_password_without_tty_exits_2():
    p = run_cli(["integrity", "http://127.0.0.1:1/"])
    assert p.returncode == 2 and "ZODAV_AUDIT_PASSWORD" in p.stderr


def test_password_never_printed(fakedav, tmp_path):
    url, srv = fakedav
    for pw, args in [
        (PASSWORD, ["conformance", url]),
        ("wrong-Sentinel-pw-77", ["conformance", url]),
        (PASSWORD, ["integrity", url, "--json"]),
        (PASSWORD, ["conformance", url, "--json"]),
    ]:
        p = run_cli(args, {"ZODAV_AUDIT_PASSWORD": pw})
        assert pw not in p.stdout + p.stderr
        token = base64.b64encode(f"{USER}:{pw}".encode()).decode()
        assert token not in p.stdout + p.stderr and "Authorization" not in p.stdout + p.stderr
    # credentials typed into the URL are not echoed either
    p = run_cli(
        ["integrity", url.replace("http://", "http://me:urlSecret99@")],
        {"ZODAV_AUDIT_PASSWORD": PASSWORD},
    )
    assert "urlSecret99" not in p.stdout + p.stderr


def test_conformance_over_cli_against_fake(fakedav):
    url, _ = fakedav
    p = run_cli(["conformance", url, "--user", USER], {"ZODAV_AUDIT_PASSWORD": PASSWORD})
    assert p.returncode == 0, p.stdout + p.stderr
    bad = run_cli(["conformance", url], {"ZODAV_AUDIT_PASSWORD": "wrong-pw"})
    assert bad.returncode == 1 and "AUTH_FAILED" in bad.stdout


def test_repair_cli_dry_run_then_apply(tmp_path, monkeypatch, capsys):
    store = build_defect_store(tmp_path / "z")
    monkeypatch.chdir(tmp_path)
    assert main(["repair", str(store)]) == 0
    assert "WOULD_WRITE_PROP" in capsys.readouterr().out
    assert not (store / "BBBB2222.prop").exists()
    main(["repair", str(store), "--apply", "--quarantine", str(tmp_path / "q")])
    assert (store / "BBBB2222.prop").exists()


def test_watch_missing_directory_exits_2(capsys):
    assert main(["watch", "/nonexistent", "--once"]) == 2
    assert "not an existing directory" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and "zodav-audit" in capsys.readouterr().out
